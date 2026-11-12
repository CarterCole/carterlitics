#!/usr/bin/env python3
"""Provision and ship carterlitics on AWS: one Lambda, one table, two buckets, and a route
on each site's CloudFront distribution.

    infra/deploy.py provision [--dry-run]     table, buckets, role, function, URL, secrets, log trigger
    infra/deploy.py code [--dry-run]          ship the collector if it changed
    infra/deploy.py routes SITE [--dry-run]   /_c/* on SITE's distribution to the collector
          [--drop '/api/*' ...]               ...and remove behaviors it replaces
    infra/deploy.py routes SITE --rollback    put SITE's distribution back as it was
    infra/deploy.py logging SITE [--dry-run]  CloudFront access logs for SITE -> the logs bucket
    infra/deploy.py register-client [--force] an OIDC client at the issuer, for the console
    infra/deploy.py server-key SITE           the key SITE's backend sends to /_c/server/events
    infra/deploy.py grant-deploy [--dry-run]  let the CI role and publishing user run `code`
    infra/deploy.py search-console [--dry-run] a Google service account that reads Search Console:
                                              its project and AWS federation for the function;
                                              which properties it reads, and which sites it can't
          [--verify-dns]                      ...and make it a verified owner of each site's domain
                                              through a TXT record in Route 53
    infra/deploy.py geo                       GeoLite2 City + ASN into geo/ (MAXMIND_ACCOUNT_ID and
                                              MAXMIND_LICENSE_KEY), or IPinfo Lite (IPINFO_TOKEN)
    infra/deploy.py intel                     published IP ranges -> geo/ranges.bin: crawlers, clouds,
                                              Private Relay, Tor, VPNs, Starlink, DROP, attackers

Settings are carterlitics.toml's [aws] table and its site list. Every step looks before it
creates, so re-running is safe. It drives the AWS CLI (no boto3) with whatever credentials
the environment selects: AWS_PROFILE=carter-admin for everything but `code`. Run as the
deploy user or role (which may only ship code), the other commands stop before touching
anything and say so; a call AWS refuses always stops, naming who made it.

Secrets live in SSM Parameter Store under [aws].params and reach the function as
environment variables. They go to the CLI in files readable only by you, never on a
command line.

`search-console` also drives Google Cloud, as gcloud's application default credentials
(`gcloud auth application-default login`): [google] in carterlitics.toml says where.
"""
from __future__ import annotations

import argparse
import base64
import difflib
import functools
import hashlib
import io
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PACKAGE = ROOT / "carterlitics"
GEO_DIR = ROOT / "geo"
RANGES = GEO_DIR / "ranges.bin"
STATE = HERE / ".state"
CONFIG = ROOT / "carterlitics.toml"

sys.path.insert(0, str(ROOT))
from carterlitics import google  # noqa: E402
from carterlitics.auth import server_key  # noqa: E402
from carterlitics.config import Config  # noqa: E402
from carterlitics.intel import Ranges, build_ranges  # noqa: E402

RAW = tomllib.loads(CONFIG.read_text())
CFG = Config.from_data(RAW)
AWS = RAW.get("aws", {})
REGION = AWS.get("region", "us-east-1")
FUNCTION = AWS.get("function", "carterlitics")
ROLE = AWS.get("role", FUNCTION)
TABLE = AWS.get("table", "carterlitics")
PARAMS = AWS.get("params", "/carterlitics").rstrip("/")
LOG_GROUP = f"/aws/lambda/{FUNCTION}"
DEPLOY_ROLE = AWS.get("deploy_role", "")
DEPLOY_USER = AWS.get("deploy_user", "")
ADMIN_PROFILE = AWS.get("admin_profile", "carter-admin")
# How the CLI says a call was refused, as opposed to failed. A refusal is never "not there".
DENIED = ("AccessDenied", "UnauthorizedOperation", "not authorized to perform", "is not authorized to")
ORIGIN_REQUEST_POLICY = AWS.get("origin_request_policy", "carterlitics")
LOG_DAYS = 7          # raw access log files: counted within minutes, kept a week for re-runs
DIRECT_UPLOAD = 45 * 1024 * 1024

# Managed CloudFront cache policies.
CACHING_DISABLED = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
CACHING_OPTIMIZED = "658327ea-f89d-4fab-a63d-7e88639e58f6"

# What the collector reads off a request. Host is left out on purpose: a Function URL
# answers only to its own. The CloudFront-* ones are set by CloudFront, not the client.
# A policy forwards at most 10 headers (an adjustable quota), so the region comes from
# GeoLite2 City; the collector reads cloudfront-viewer-country-region-name if it's sent.
FORWARDED_HEADERS = [
    "origin", "user-agent", "sec-gpc", "dnt", "x-carterlitics-key",
    "cloudfront-viewer-address", "cloudfront-viewer-country", "cloudfront-viewer-asn", "cloudfront-viewer-city",
    "cloudfront-viewer-time-zone",
]
MAX_POLICY_HEADERS = 10
ORIGIN_ID = "carterlitics"
# The access log fields the collector reads (no cookies: they never leave CloudFront).
LOG_FIELDS = ["timestamp(ms)", "c-ip", "cs-method", "x-host-header", "cs-uri-stem", "cs-uri-query", "sc-status",
              "cs(Referer)", "cs(User-Agent)", "sc-bytes", "x-edge-result-type", "time-taken", "c-country", "asn"]


class Fail(Exception):
    pass


def log(step: str, msg: str) -> None:
    print(f"  {step:<9} {msg}", flush=True)


@functools.cache
def caller() -> str:
    """Who the CLI acts as, for messages. Straight to the CLI: never refused, never recursive."""
    proc = subprocess.run(["aws", "sts", "get-caller-identity", "--query", "Arn", "--output", "text",
                           "--region", REGION], capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else "credentials the CLI can't identify"


def rerun_as_admin() -> str:
    return f"AWS_PROFILE={ADMIN_PROFILE} infra/deploy.py {' '.join(shlex.quote(a) for a in sys.argv[1:])}"


def refused(args, stderr: str) -> Fail:
    reason = stderr.strip().splitlines()[-1] if stderr.strip() else ""
    if sys.argv[1:2] == ["code"]:
        hint = (f"`code` runs as {DEPLOY_USER or 'the deploy user'} or {DEPLOY_ROLE or 'the deploy role'} once "
                f"`AWS_PROFILE={ADMIN_PROFILE} infra/deploy.py grant-deploy` has allowed it; or: {rerun_as_admin()}")
    else:
        hint = f"Commands other than `code` need administrator credentials: {rerun_as_admin()}"
    return Fail(f"AWS refused `aws {' '.join(args[:2])}`: this shell acts as {caller()}.\n{reason}\n{hint}")


def require_admin(command: str) -> None:
    """Stop before anything happens when the shell holds the deploy user's or role's
    credentials, which may only ship code."""
    arn = caller()
    if (DEPLOY_USER and arn.endswith(f":user/{DEPLOY_USER}")) or \
            (DEPLOY_ROLE and f":assumed-role/{DEPLOY_ROLE}/" in arn):
        raise Fail(f"`{command}` needs administrator credentials, but this shell acts as {arn}, which may "
                   f"only run `code`.\nRun: {rerun_as_admin()}")


def aws(*args: str, missing: tuple[str, ...] = (), payload: dict | None = None):
    """Run the CLI and parse its JSON. A failure whose error names one of the codes in
    `missing` (ResourceNotFoundException, NoSuchEntity, …) returns None: the thing isn't
    there. Any other failure raises, a refusal with who was refused. `payload` goes in via
    --cli-input-json from a file readable only by us, which is how secrets travel."""
    cmd = ["aws", *args, "--region", REGION, "--output", "json"]
    tmp = None
    try:
        if payload is not None:
            fd, tmp = tempfile.mkstemp(suffix=".json")
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f)
            cmd += ["--cli-input-json", f"file://{tmp}"]
        proc = subprocess.run(cmd, capture_output=True, text=True)
    finally:
        if tmp:
            os.unlink(tmp)
    if proc.returncode != 0:
        if any(d in proc.stderr for d in DENIED):
            raise refused(args, proc.stderr)
        if any(code in proc.stderr for code in missing):
            return None
        raise Fail(f"aws {' '.join(args[:2])} failed:\n{proc.stderr.strip()}")
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def error_of(*args: str) -> str:
    """The CLI's error output ("" on success), for callers that expect some failures
    (not found, already exists). A refusal is never one of those, so it raises."""
    proc = subprocess.run(["aws", *args, "--region", REGION, "--output", "json"], capture_output=True, text=True)
    if proc.returncode != 0 and any(d in proc.stderr for d in DENIED):
        raise refused(args, proc.stderr)
    return "" if proc.returncode == 0 else proc.stderr


@functools.cache
def account() -> str:
    return aws("sts", "get-caller-identity")["Account"]


@functools.cache
def bucket() -> str:
    """Recordings (and large code packages). Bucket names are global: the account id keeps it ours."""
    return AWS.get("bucket") or f"carterlitics-{account()}"


@functools.cache
def logs_bucket() -> str:
    return AWS.get("logs_bucket") or f"{bucket()}-logs"


@functools.cache
def all_distributions() -> list[dict]:
    return aws("cloudfront", "list-distributions")["DistributionList"].get("Items", [])


def distributions(site) -> list[str]:
    """The site's CloudFront distributions: the one it names, or every one aliased to
    one of its host names (resume.cartercole.net and .com are two)."""
    if site.distribution:
        return [site.distribution]
    names = {site.id, *site.aliases} | (set() if site.id.startswith("*.") else {site.host})
    found = [d["Id"] for d in all_distributions() if names & set(d.get("Aliases", {}).get("Items", []))]
    if not found:
        raise Fail(f"no CloudFront distribution is aliased to {', '.join(sorted(names))}")
    return found


def site_of(name: str):
    site = CFG.sites.get(name)
    if not site:
        raise Fail(f"no site {name!r} in carterlitics.toml (have: {', '.join(CFG.sites)})")
    return site


# ---------------------------------------------------------------- code


def package() -> bytes:
    """The collector and its static files, plus any .mmdb and the ranges table in geo/.
    Byte-identical for identical inputs, so CodeSha256 says whether Lambda already runs it."""
    files = sorted(p for p in PACKAGE.rglob("*") if p.is_file() and "__pycache__" not in p.parts
                   and not p.name.startswith("."))
    files += sorted(GEO_DIR.glob("*.mmdb")) if GEO_DIR.exists() else []
    files += [RANGES] if RANGES.exists() else []
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path in files:
            info = zipfile.ZipInfo(path.relative_to(ROOT).as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, path.read_bytes())
    return buf.getvalue()


def sha(blob: bytes) -> str:
    return base64.b64encode(hashlib.sha256(blob).digest()).decode()


def wait_updated() -> None:
    """Until the last change has landed. Polls GetFunctionConfiguration, the one read
    the deploy role is allowed (the CLI's waiters call GetFunction)."""
    for _ in range(90):
        cfg = aws("lambda", "get-function-configuration", "--function-name", FUNCTION)
        if cfg.get("State") == "Active" and cfg.get("LastUpdateStatus") in ("Successful", None):
            return
        if cfg.get("LastUpdateStatus") == "Failed" or cfg.get("State") == "Failed":
            raise Fail(f"{FUNCTION} update failed: {cfg.get('LastUpdateStatusReason') or cfg.get('StateReason')}")
        time.sleep(2)
    raise Fail(f"timed out waiting for {FUNCTION} to update")


def code_args(blob: bytes) -> list[str]:
    """--zip-file for a small package; through the bucket past Lambda's direct upload cap."""
    if len(blob) <= DIRECT_UPLOAD:
        path = Path(tempfile.mkdtemp()) / "carterlitics.zip"
        path.write_bytes(blob)
        return ["--zip-file", f"fileb://{path}"]
    key = f"code/{hashlib.sha256(blob).hexdigest()[:16]}.zip"
    path = Path(tempfile.mkdtemp()) / "carterlitics.zip"
    path.write_bytes(blob)
    aws("s3api", "put-object", "--bucket", bucket(), "--key", key, "--body", str(path))
    return ["--s3-bucket", bucket(), "--s3-key", key]


def ship_code(dry: bool) -> None:
    blob = package()
    err = error_of("lambda", "get-function-configuration", "--function-name", FUNCTION)
    if "ResourceNotFoundException" in err:
        log("code", f"{FUNCTION} does not exist yet; run `infra/deploy.py provision` first. Skipping.")
        return
    if err:
        raise Fail(f"cannot read {FUNCTION}:\n{err.strip()}")
    live = aws("lambda", "get-function-configuration", "--function-name", FUNCTION)["CodeSha256"]
    if live == sha(blob):
        log("code", f"{FUNCTION} already runs this ({len(blob) // 1024} KB)")
        return
    if dry:
        log("code", f"would update {FUNCTION} ({len(blob) // 1024} KB)")
        return
    aws("lambda", "update-function-code", "--function-name", FUNCTION, *code_args(blob))
    wait_updated()
    log("code", f"updated {FUNCTION} ({len(blob) // 1024} KB)")


# ---------------------------------------------------------------- secrets


def param(name: str) -> str | None:
    got = aws("ssm", "get-parameter", "--name", f"{PARAMS}/{name}", "--with-decryption",
              missing=("ParameterNotFound",))
    return got["Parameter"]["Value"] if got else None


def put_param(name: str, value: str) -> None:
    aws("ssm", "put-parameter", payload={"Name": f"{PARAMS}/{name}", "Value": value,
                                         "Type": "SecureString", "Overwrite": True})


def ensure_secret(name: str, dry: bool) -> str:
    value = param(name)
    if value:
        log("secret", f"{PARAMS}/{name} exists")
        return value
    if dry:
        log("secret", f"would generate {PARAMS}/{name}")
        return "DRY-RUN"
    put_param(name, secrets.token_hex(32))
    log("secret", f"generated {PARAMS}/{name}")
    return param(name)


# ---------------------------------------------------------------- provision


def ensure_table(dry: bool) -> str:
    got = aws("dynamodb", "describe-table", "--table-name", TABLE, missing=("ResourceNotFoundException",))
    if got:
        log("table", f"{TABLE} exists")
        arn = got["Table"]["TableArn"]
    elif dry:
        log("table", f"would create {TABLE} (on-demand, pk/sk, TTL on expires)")
        return f"arn:aws:dynamodb:{REGION}:000000000000:table/{TABLE}"
    else:
        arn = aws("dynamodb", "create-table", "--table-name", TABLE, "--billing-mode", "PAY_PER_REQUEST",
                  "--attribute-definitions", "AttributeName=pk,AttributeType=S", "AttributeName=sk,AttributeType=S",
                  "--key-schema", "AttributeName=pk,KeyType=HASH", "AttributeName=sk,KeyType=RANGE",
                  )["TableDescription"]["TableArn"]
        aws("dynamodb", "wait", "table-exists", "--table-name", TABLE)
        log("table", f"created {TABLE}")
    ttl = aws("dynamodb", "describe-time-to-live", "--table-name", TABLE)["TimeToLiveDescription"]
    if ttl.get("TimeToLiveStatus") in ("ENABLED", "ENABLING"):
        log("table", "TTL on `expires` is on")
    elif not dry:
        aws("dynamodb", "update-time-to-live", "--table-name", TABLE,
            "--time-to-live-specification", "Enabled=true,AttributeName=expires")
        log("table", "turned on TTL for `expires`")
    return arn


def ensure_bucket(name: str, acct: str, lifecycle: list[dict], extra_policy: list[dict], dry: bool) -> str:
    """Private, encrypted, TLS only, and emptied by its lifecycle rules."""
    err = error_of("s3api", "head-bucket", "--bucket", name, "--expected-bucket-owner", acct)
    if not err:
        log("bucket", f"s3://{name} exists")
    elif "403" in err or "Forbidden" in err:
        # HeadBucket's 403 says nothing more: another account's bucket, or one we may not read.
        raise Fail(f"s3://{name} answers 403 to {caller()}: either another account owns it (pick another "
                   f"name in carterlitics.toml) or these credentials may not read it ({rerun_as_admin()})")
    elif dry:
        log("bucket", f"would create s3://{name} (private, SSE-S3, TLS only)")
        return f"arn:aws:s3:::{name}"
    else:
        aws("s3api", "create-bucket", "--bucket", name)  # us-east-1 takes no LocationConstraint
        log("bucket", f"created s3://{name}")
    arn = f"arn:aws:s3:::{name}"
    if dry:
        return arn
    # Applied on every run, so the bucket converges on this however it started.
    aws("s3api", "put-public-access-block", "--bucket", name, "--public-access-block-configuration",
        "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true")
    aws("s3api", "put-bucket-ownership-controls", "--bucket", name,
        "--ownership-controls", "Rules=[{ObjectOwnership=BucketOwnerEnforced}]")
    aws("s3api", "put-bucket-encryption", "--bucket", name, "--server-side-encryption-configuration",
        json.dumps({"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]}))
    aws("s3api", "put-bucket-lifecycle-configuration", "--bucket", name,
        "--lifecycle-configuration", json.dumps({"Rules": lifecycle}))
    aws("s3api", "put-bucket-policy", "--bucket", name, "--policy", json.dumps({
        "Version": "2012-10-17", "Statement": [{
            "Sid": "TlsOnly", "Effect": "Deny", "Principal": "*", "Action": "s3:*",
            "Resource": [arn, f"{arn}/*"], "Condition": {"Bool": {"aws:SecureTransport": "false"}}},
            *extra_policy]}))
    log("bucket", f"s3://{name}: private, SSE-S3, TLS only, " +
        ", ".join(f"{r['Filter'].get('Prefix') or 'everything'} expires after {r['Expiration']['Days']} days"
                  for r in lifecycle))
    return arn


def expire(rule_id: str, prefix: str, days: int) -> dict:
    return {"ID": rule_id, "Status": "Enabled", "Filter": {"Prefix": prefix}, "Expiration": {"Days": days},
            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1}}


def ensure_logs(dry: bool) -> None:
    groups = aws("logs", "describe-log-groups", "--log-group-name-prefix", LOG_GROUP)["logGroups"]
    if any(g["logGroupName"] == LOG_GROUP for g in groups):
        log("logs", f"{LOG_GROUP} exists")
    elif dry:
        log("logs", f"would create {LOG_GROUP}")
        return
    else:
        aws("logs", "create-log-group", "--log-group-name", LOG_GROUP)
        log("logs", f"created {LOG_GROUP}")
    if not dry:
        aws("logs", "put-retention-policy", "--log-group-name", LOG_GROUP, "--retention-in-days", "14")


def ensure_role(table_arn: str, bucket_arn: str, logs_arn: str, acct: str, dry: bool) -> str:
    got = aws("iam", "get-role", "--role-name", ROLE, missing=("NoSuchEntity",))
    if got:
        log("role", f"{ROLE} exists")
        arn = got["Role"]["Arn"]
    elif dry:
        log("role", f"would create {ROLE}")
        return f"arn:aws:iam::{acct}:role/{ROLE}"
    else:
        trust = {"Version": "2012-10-17", "Statement": [{
            "Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
        arn = aws("iam", "create-role", "--role-name", ROLE, "--assume-role-policy-document", json.dumps(trust),
                  "--description", "carterlitics: its table, its buckets, its logs")["Role"]["Arn"]
        log("role", f"created {ROLE}")
    policy = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["dynamodb:UpdateItem", "dynamodb:PutItem", "dynamodb:GetItem",
                                       "dynamodb:Query", "dynamodb:BatchWriteItem", "dynamodb:BatchGetItem"],
         "Resource": table_arn},
        {"Effect": "Allow", "Action": ["s3:PutObject", "s3:GetObject"], "Resource": f"{bucket_arn}/rec/*"},
        {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": bucket_arn,
         "Condition": {"StringLike": {"s3:prefix": "rec/*"}}},
        {"Effect": "Allow", "Action": "s3:GetObject", "Resource": f"{logs_arn}/*"},
        {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": [f"arn:aws:logs:{REGION}:{acct}:log-group:{LOG_GROUP}",
                      f"arn:aws:logs:{REGION}:{acct}:log-group:{LOG_GROUP}:*"]},
    ]}
    if not dry:
        aws("iam", "put-role-policy", "--role-name", ROLE, "--policy-name", "carterlitics",
            "--policy-document", json.dumps(policy))
    return arn


def environment(dry: bool) -> dict:
    client_id, client_secret = param("oidc-client-id"), param("oidc-client-secret")
    if not client_id:
        log("signin", "no OIDC client yet: the console's sign-in stays off until `register-client`")
    env = {
        "CARTERLITICS_CONFIG": json.dumps(CFG.to_data(), separators=(",", ":")),
        "TABLE": TABLE,
        "BUCKET": bucket(),
        "GEO_MMDB": ",".join(f"/var/task/geo/{p.name}" for p in sorted(GEO_DIR.glob("*.mmdb")))
        if GEO_DIR.exists() else "",
        "INTEL_RANGES": f"/var/task/geo/{RANGES.name}" if RANGES.exists() else "",
        "ORIGIN_SECRET": ensure_secret("origin-secret", dry),
        "SESSION_SECRET": ensure_secret("session-secret", dry),
        "SERVER_SECRET": ensure_secret("server-secret", dry),
        "OIDC_CLIENT_ID": client_id or "",
        "OIDC_CLIENT_SECRET": client_secret or "",
        "ADMIN_IDS": param("admin-ids") or "",
        "GOOGLE_AUDIENCE": param("google-audience") or "",
    }
    if CFG.google_account and not env["GOOGLE_AUDIENCE"]:
        log("search", "no Google federation yet: the Search tab stays off until `search-console`")
    size = sum(len(k) + len(v) for k, v in env.items())
    if size > 4000:
        raise Fail(f"environment is {size} bytes; Lambda allows 4 KB. Trim carterlitics.toml.")
    return env


def ensure_function(role_arn: str, env: dict, dry: bool) -> str:
    config = {
        "FunctionName": FUNCTION, "Role": role_arn, "Runtime": "python3.13",
        "Handler": "carterlitics.app.lambda_handler", "MemorySize": 512, "Timeout": 60,
        "Environment": {"Variables": env},
        "Description": f"carterlitics: first-party analytics for {len(CFG.sites)} sites (console on {CFG.console})"[:256],
        "LoggingConfig": {"LogFormat": "Text", "LogGroup": LOG_GROUP},
    }
    err = error_of("lambda", "get-function-configuration", "--function-name", FUNCTION)
    if err and "ResourceNotFoundException" not in err:
        raise Fail(f"cannot read {FUNCTION}:\n{err.strip()}")
    exists = not err
    if dry:
        log("function", f"would {'update' if exists else 'create'} {FUNCTION} (python3.13, arm64)")
        return f"arn:aws:lambda:{REGION}:000000000000:function:{FUNCTION}"
    if exists:
        aws("lambda", "update-function-configuration", payload=config)
        wait_updated()
        log("function", f"updated {FUNCTION} configuration")
        ship_code(dry)
    else:
        blob = package()
        code = code_args(blob)
        create = {**config, "Architectures": ["arm64"],
                  "Code": {"ZipFile": base64.b64encode(blob).decode()} if code[0] == "--zip-file"
                  else {"S3Bucket": code[1], "S3Key": code[3]}}
        # A role made a moment ago takes a few seconds before Lambda may assume it.
        for attempt in range(12):
            wait = ("cannot be assumed by Lambda",) if attempt < 11 else ()
            if aws("lambda", "create-function", payload=create, missing=wait) is not None:
                break
            time.sleep(5)
        wait_updated()
        log("function", f"created {FUNCTION}")
    return aws("lambda", "get-function-configuration", "--function-name", FUNCTION)["FunctionArn"]


def ensure_url(dry: bool) -> str:
    got = aws("lambda", "get-function-url-config", "--function-name", FUNCTION,
              missing=("ResourceNotFoundException",))
    if got:
        log("url", got["FunctionUrl"])
        url = got["FunctionUrl"]
    elif dry:
        log("url", "would create a Function URL (auth NONE; CloudFront's secret header guards it)")
        return "https://dry-run.lambda-url.us-east-1.on.aws/"
    else:
        url = aws("lambda", "create-function-url-config", "--function-name", FUNCTION,
                  "--auth-type", "NONE")["FunctionUrl"]
        log("url", f"created {url}")
    if dry:
        return url
    # A public Function URL needs both: InvokeFunctionUrl, and InvokeFunction scoped to the URL.
    for sid, extra in (("FunctionURLAllowPublicAccess", ["--action", "lambda:InvokeFunctionUrl",
                                                         "--function-url-auth-type", "NONE"]),
                       ("FunctionURLInvokeAllowPublicAccess", ["--action", "lambda:InvokeFunction",
                                                               "--invoked-via-function-url"])):
        err = error_of("lambda", "add-permission", "--function-name", FUNCTION, "--statement-id", sid,
                       "--principal", "*", *extra)
        if err and "ResourceConflictException" not in err:
            raise Fail(f"add-permission {sid}:\n{err.strip()}")
    return url


def ensure_trigger(function_arn: str, acct: str, dry: bool) -> None:
    """New access log files in the logs bucket invoke the function."""
    if dry:
        log("trigger", f"would send s3://{logs_bucket()} ObjectCreated events to {FUNCTION}")
        return
    err = error_of("lambda", "add-permission", "--function-name", FUNCTION, "--statement-id", "s3-access-logs",
                   "--principal", "s3.amazonaws.com", "--action", "lambda:InvokeFunction",
                   "--source-arn", f"arn:aws:s3:::{logs_bucket()}", "--source-account", acct)
    if err and "ResourceConflictException" not in err:
        raise Fail(f"add-permission s3-access-logs:\n{err.strip()}")
    aws("s3api", "put-bucket-notification-configuration", "--bucket", logs_bucket(),
        "--notification-configuration", json.dumps({"LambdaFunctionConfigurations": [{
            "Id": "carterlitics-access-logs", "LambdaFunctionArn": function_arn,
            "Events": ["s3:ObjectCreated:*"]}]}))
    log("trigger", f"s3://{logs_bucket()} -> {FUNCTION}")


def provision(dry: bool) -> None:
    acct = account()
    table_arn = ensure_table(dry)
    bucket_arn = ensure_bucket(bucket(), acct, [expire("recordings", "rec/", CFG.retention["recordings"]),
                                              expire("code", "code/", 30)], [], dry)
    # CloudWatch Logs delivers CloudFront's v2 access logs as this service principal.
    delivery = [{
        "Sid": "AWSLogDeliveryWrite", "Effect": "Allow", "Principal": {"Service": "delivery.logs.amazonaws.com"},
        "Action": "s3:PutObject", "Resource": f"arn:aws:s3:::{logs_bucket()}/*",
        "Condition": {"StringEquals": {"s3:x-amz-acl": "bucket-owner-full-control", "aws:SourceAccount": acct},
                      "ArnLike": {"aws:SourceArn": f"arn:aws:logs:{REGION}:{acct}:delivery-source:*"}}}]
    logs_arn = ensure_bucket(logs_bucket(), acct, [expire("access-logs", "", LOG_DAYS)], delivery, dry)
    ensure_logs(dry)
    role_arn = ensure_role(table_arn, bucket_arn, logs_arn, acct, dry)
    function_arn = ensure_function(role_arn, environment(dry), dry)
    ensure_url(dry)
    ensure_trigger(function_arn, acct, dry)


# ---------------------------------------------------------------- routes


def canonical(policy: dict) -> str:
    """A policy config as CloudFront compares it: header names in any order or case."""
    p = json.loads(json.dumps(policy))
    items = p.get("HeadersConfig", {}).get("Headers", {}).get("Items")
    if items:
        p["HeadersConfig"]["Headers"]["Items"] = sorted(h.lower() for h in items)
    return json.dumps(p, sort_keys=True)


def ensure_origin_request_policy(dry: bool) -> str:
    if len(FORWARDED_HEADERS) > MAX_POLICY_HEADERS:
        raise Fail(f"{len(FORWARDED_HEADERS)} forwarded headers; an origin request policy takes "
                   f"{MAX_POLICY_HEADERS} unless the quota is raised")
    listing = aws("cloudfront", "list-origin-request-policies", "--type", "custom")["OriginRequestPolicyList"]
    config = {
        "Name": ORIGIN_REQUEST_POLICY,
        "Comment": "carterlitics: cookies, query, and the headers the collector reads. No Host.",
        "HeadersConfig": {"HeaderBehavior": "whitelist",
                          "Headers": {"Quantity": len(FORWARDED_HEADERS), "Items": FORWARDED_HEADERS}},
        "CookiesConfig": {"CookieBehavior": "all"},
        "QueryStringsConfig": {"QueryStringBehavior": "all"},
    }
    for item in listing.get("Items", []):
        policy = item["OriginRequestPolicy"]
        if policy["OriginRequestPolicyConfig"]["Name"] == ORIGIN_REQUEST_POLICY:
            live = policy["OriginRequestPolicyConfig"]
            if canonical(live) == canonical(config):
                log("policy", f"origin request policy {policy['Id']}")
                return policy["Id"]
            had = {h.lower() for h in live.get("HeadersConfig", {}).get("Headers", {}).get("Items") or []}
            change = ", ".join([f"+{h}" for h in FORWARDED_HEADERS if h not in had] +
                               [f"-{h}" for h in sorted(had - set(FORWARDED_HEADERS))]) or "settings"
            if dry:
                log("policy", f"would update origin request policy {policy['Id']} ({change})")
            else:
                etag = aws("cloudfront", "get-origin-request-policy", "--id", policy["Id"])["ETag"]
                aws("cloudfront", "update-origin-request-policy", "--id", policy["Id"], "--if-match", etag,
                    "--origin-request-policy-config", json.dumps(config))
                log("policy", f"updated origin request policy {policy['Id']} ({change})")
            return policy["Id"]
    if dry:
        log("policy", f"would create origin request policy {ORIGIN_REQUEST_POLICY}")
        return "DRY-RUN-POLICY"
    pid = aws("cloudfront", "create-origin-request-policy",
              "--origin-request-policy-config", json.dumps(config))["OriginRequestPolicy"]["Id"]
    log("policy", f"created origin request policy {pid}")
    return pid


def behavior(path: str, cache: str, request_policy: str | None, methods: list[str]) -> dict:
    out = {
        "PathPattern": path, "TargetOriginId": ORIGIN_ID,
        "TrustedSigners": {"Enabled": False, "Quantity": 0},
        "TrustedKeyGroups": {"Enabled": False, "Quantity": 0},
        "ViewerProtocolPolicy": "redirect-to-https",
        "AllowedMethods": {"Quantity": len(methods), "Items": methods,
                           "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]}},
        "SmoothStreaming": False, "Compress": True,
        "LambdaFunctionAssociations": {"Quantity": 0}, "FunctionAssociations": {"Quantity": 0},
        "FieldLevelEncryptionId": "", "CachePolicyId": cache,
    }
    if request_policy:
        out["OriginRequestPolicyId"] = request_policy
    return out


def routed(config: dict, site, api_host: str, secret: str, policy_id: str, drop: list[str]) -> dict:
    """The distribution config with the collector's origin and behaviors, first, and
    nothing else changed except `drop`. Anything already ours is replaced, so this converges."""
    new = json.loads(json.dumps(config))
    origin = {
        "Id": ORIGIN_ID, "DomainName": api_host, "OriginPath": "",
        "CustomHeaders": {"Quantity": 2, "Items": [
            {"HeaderName": "x-origin-verify", "HeaderValue": secret},
            {"HeaderName": "x-carterlitics-site", "HeaderValue": site.id}]},
        "CustomOriginConfig": {"HTTPPort": 80, "HTTPSPort": 443, "OriginProtocolPolicy": "https-only",
                               "OriginSslProtocols": {"Quantity": 1, "Items": ["TLSv1.2"]},
                               "OriginReadTimeout": 30, "OriginKeepaliveTimeout": 5},
        "ConnectionAttempts": 3, "ConnectionTimeout": 10,
        "OriginShield": {"Enabled": False}, "OriginAccessControlId": "",
    }
    live_origins = new["Origins"].get("Items", [])
    origin = keep_live(live_origins, "Id", origin)
    # Where it already is (a site's own deploy may put it anywhere), else last.
    if any(o["Id"] == ORIGIN_ID for o in live_origins):
        origins = [origin if o["Id"] == ORIGIN_ID else o for o in live_origins]
    else:
        origins = live_origins + [origin]
    new["Origins"] = {"Quantity": len(origins), "Items": origins}
    mount = CFG.mount
    read = ["GET", "HEAD"]
    every = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    ours = [
        # Static and content-hashed: cached at the edge, compressed there too.
        behavior(f"{mount}/c.js", CACHING_OPTIMIZED, None, read),
        behavior(f"{mount}/s/*", CACHING_OPTIMIZED, None, read),
        # Beacons, the console and its API: never cached, with the headers the collector reads.
        behavior(f"{mount}/*", CACHING_DISABLED, policy_id, every),
    ]
    live_behaviors = new.get("CacheBehaviors", {}).get("Items", [])
    ours = [keep_live(live_behaviors, "PathPattern", b) for b in ours]
    paths = {b["PathPattern"] for b in ours}
    rest = [b for b in live_behaviors if b["PathPattern"] not in paths and b["PathPattern"] not in drop]
    behaviors = ours + rest
    new["CacheBehaviors"] = {"Quantity": len(behaviors), "Items": behaviors}
    return new


def normal(x):
    """A config fragment as CloudFront means it: lists of names (methods, protocols) in any
    order, custom headers in any order, and gRPC off whether or not it says so."""
    if isinstance(x, dict):
        return {k: normal(v) for k, v in x.items() if not (k == "GrpcConfig" and v == {"Enabled": False})}
    if isinstance(x, list):
        items = [normal(v) for v in x]
        if all(isinstance(v, str) for v in items):
            return sorted(items)
        if all(isinstance(v, dict) and "HeaderName" in v for v in items):
            return sorted(items, key=lambda v: v["HeaderName"])
        return items
    return x


def keep_live(live: list[dict], key: str, wanted: dict) -> dict:
    """`wanted`, or the live item with the same key when the two mean the same thing, so a
    re-run that changes nothing shows no diff and leaves the distribution alone."""
    twin = next((item for item in live if item.get(key) == wanted[key]), None)
    return twin if twin is not None and normal(twin) == normal(wanted) else wanted


def show_diff(before: dict, after: dict) -> bool:
    def lines(c):
        text = json.dumps(c, indent=2, sort_keys=True).splitlines()
        return [line.split(":")[0] + ': "<redacted>"' if '"HeaderValue"' in line else line for line in text]
    diff = list(difflib.unified_diff(lines(before), lines(after), "live", "new", lineterm="", n=2))
    print("\n".join(diff) if diff else "  (no change)")
    return bool(diff)


def targets(name: str, clients_only: bool = False) -> list:
    """`all` is every site (with `client`, for routes); otherwise the one named."""
    if name != "all":
        site = site_of(name)
        if clients_only and not site.client:
            raise Fail(f"{site.id} has client = false: its pages don't load c.js, so it needs no route")
        return [site]
    return [s for s in CFG.sites.values() if s.client or not clients_only]


def routes(name: str, dry: bool, rollback: bool, drop: list[str]) -> None:
    sites = targets(name, clients_only=True)
    # The policy first: updating it changes every distribution that uses it (and their
    # ETags), so a distribution read before it could no longer be written.
    policy_id = None if rollback else ensure_origin_request_policy(dry)
    for site in sites:
        for dist in distributions(site):
            log("cdn", f"{site.id}: distribution {dist}")
            route_one(site, dist, dry, rollback, drop, policy_id)


def update_distribution(dist: str, etag: str, live: dict, new: dict) -> None:
    """update-distribution, with the config (it holds the origin secret) in a private file.
    If CloudFront moved the ETag on without changing the config, as a policy update can a
    moment later, once more with the new one; if the config itself changed, stop."""
    for attempt in range(2):
        # PreconditionFailed comes back as None: the ETag is stale.
        done = aws("cloudfront", "update-distribution", payload={"Id": dist, "IfMatch": etag,
                                                                 "DistributionConfig": new},
                   missing=("PreconditionFailed",) if not attempt else ())
        if done is not None:
            return
        got = aws("cloudfront", "get-distribution-config", "--id", dist)
        if got["DistributionConfig"] != live:
            raise Fail(f"{dist} changed while this ran; run it again to see the new diff")
        etag = got["ETag"]


def route_one(site, dist: str, dry: bool, rollback: bool, drop: list[str], policy_id: str | None) -> None:
    got = aws("cloudfront", "get-distribution-config", "--id", dist)
    etag, live = got["ETag"], got["DistributionConfig"]
    backup = STATE / f"{dist}-before-carterlitics.json"

    if rollback:
        if not backup.exists():
            raise Fail(f"no backup at {backup.relative_to(ROOT)}")
        before = json.loads(backup.read_text())
        changed = show_diff(live, before)
        if dry or not changed:
            return
        update_distribution(dist, etag, live, before)
        log("cdn", f"{dist} restored from {backup.name}; deploying (a few minutes)")
        return

    url = aws("lambda", "get-function-url-config", "--function-name", FUNCTION)["FunctionUrl"]
    api_host = url.split("//", 1)[1].rstrip("/")
    secret = param("origin-secret")
    if not secret:
        raise Fail("no origin secret: run `infra/deploy.py provision` first")
    new = routed(live, site, api_host, secret, policy_id, drop)
    if not show_diff(live, new) or dry:
        return
    # Keep the first backup: a later apply would otherwise replace the way back.
    if not backup.exists():
        STATE.mkdir(exist_ok=True)
        backup.write_text(json.dumps(live, indent=2))
        os.chmod(backup, 0o600)
    update_distribution(dist, etag, live, new)
    log("cdn", f"{dist} now routes {CFG.mount}/* to {FUNCTION}; deploying (a few minutes)")
    log("cdn", f"roll back with: infra/deploy.py routes {site.id} --rollback")


# ---------------------------------------------------------------- access logs


def logging(name: str, dry: bool) -> None:
    """CloudFront standard logging (v2): JSON lines, delivered through CloudWatch Logs to
    the logs bucket, one folder per distribution. Each new file invokes the collector."""
    for site in targets(name):
        for dist in distributions(site):
            log_one(site, dist, dry)


def log_one(site, dist: str, dry: bool) -> None:
    acct = account()
    source, dest = f"carterlitics-{dist}", "carterlitics-access-logs"
    if dry:
        log("logging", f"would deliver {dist} ({site.id}) access logs as JSON to s3://{logs_bucket()}/{dist}/")
        return
    aws("logs", "put-delivery-source", "--name", source, "--log-type", "ACCESS_LOGS",
        "--resource-arn", f"arn:aws:cloudfront::{acct}:distribution/{dist}")
    destination = aws("logs", "put-delivery-destination", "--name", dest, "--output-format", "json",
                      "--delivery-destination-configuration",
                      f"destinationResourceArn=arn:aws:s3:::{logs_bucket()}")["deliveryDestination"]["arn"]
    existing = aws("logs", "describe-deliveries")["deliveries"]
    if any(d.get("deliverySourceName") == source for d in existing):
        log("logging", f"{dist} already delivers to s3://{logs_bucket()}")
        return
    aws("logs", "create-delivery", "--delivery-source-name", source, "--delivery-destination-arn", destination,
        "--record-fields", *LOG_FIELDS,
        # JSON, not shorthand: the CLI's shorthand parser reads the {braces} as structure.
        "--s3-delivery-configuration", json.dumps({"suffixPath": "{DistributionId}/{yyyy}/{MM}/{dd}/{HH}",
                                                   "enableHiveCompatiblePath": False}))
    log("logging", f"{dist} ({site.id}) -> s3://{logs_bucket()}/{dist}/ (JSON); the first files take a few minutes")


# ---------------------------------------------------------------- clients and keys


def register_client(force: bool) -> None:
    if param("oidc-client-id") and not force:
        log("client", f"already registered ({PARAMS}/oidc-client-id); --force registers a new one")
        return
    console = CFG.console_site
    body = json.dumps({
        "client_name": "carterlitics",
        "redirect_uris": [CFG.redirect_uri],
        "post_logout_redirect_uris": [console.url.rstrip("/") + "/"],
        "token_endpoint_auth_method": "client_secret_basic",
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "subject_type": "public",
    })
    # The registration token never leaves the hub's machine: curl runs there.
    remote = ("set -a; . ~/kith-hub/hub.env; set +a; "
              f"curl -sS --fail-with-body {CFG.issuer}/oauth/register "
              '-H "authorization: Bearer $KITH_REGISTRATION_TOKEN" -H "content-type: application/json" -d @-')
    host = AWS.get("hub_ssh")
    if not host:
        raise Fail("set [aws] hub_ssh to the ssh host the issuer runs on")
    proc = subprocess.run(["ssh", "-o", "BatchMode=yes", host, remote], input=body, capture_output=True, text=True)
    if proc.returncode != 0:
        raise Fail(f"registration failed:\n{proc.stdout.strip()}\n{proc.stderr.strip()}")
    reply = json.loads(proc.stdout)
    put_param("oidc-client-id", reply["client_id"])
    put_param("oidc-client-secret", reply["client_secret"])
    log("client", f"registered {reply['client_id']}; id and secret are in {PARAMS}/oidc-client-*")
    log("client", "run `infra/deploy.py provision` to hand them to the function")


def print_server_key(name: str) -> None:
    site = site_of(name)
    secret = param("server-secret")
    if not secret:
        raise Fail("no server secret: run `infra/deploy.py provision` first")
    print(server_key(secret, site.id))


def grant_deploy(acct: str, dry: bool) -> None:
    policy = json.dumps({"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Action": ["lambda:UpdateFunctionCode", "lambda:GetFunctionConfiguration"],
        "Resource": f"arn:aws:lambda:{REGION}:{acct}:function:{FUNCTION}"}]})
    who = [w for w in (DEPLOY_ROLE and ("role", DEPLOY_ROLE), DEPLOY_USER and ("user", DEPLOY_USER)) if w]
    if dry or not who:
        log("deploy", f"would let {', '.join(n for _, n in who) or 'nobody (set deploy_role/deploy_user)'} "
            f"update {FUNCTION}'s code")
        return
    for kind, name in who:
        aws("iam", f"put-{kind}-policy", f"--{kind}-name", name, "--policy-name", "carterlitics-code",
            "--policy-document", policy)
    log("deploy", f"{', '.join(n for _, n in who)} may update {FUNCTION}'s code, nothing else")


# ---------------------------------------------------------------- search console

GOOGLE_CFG = RAW.get("google", {})
GOOGLE_PROJECT = GOOGLE_CFG.get("project", "")
GOOGLE_POOL = GOOGLE_CFG.get("pool", "carterlitics")
GOOGLE_PROVIDER = GOOGLE_CFG.get("provider", "aws-lambda")  # 4 to 32 characters
GOOGLE_SERVICES = ["iam.googleapis.com", "iamcredentials.googleapis.com", "sts.googleapis.com",
                   "searchconsole.googleapis.com", "siteverification.googleapis.com"]
CRM = "https://cloudresourcemanager.googleapis.com/v3"
USAGE = "https://serviceusage.googleapis.com/v1"
IAM = "https://iam.googleapis.com/v1"
SITE_VERIFICATION = "https://www.googleapis.com/siteVerification/v1"
NO_PROJECT = "000000000000"     # a dry run's project number, before the project exists


@functools.cache
def gcloud_token() -> str:
    token, _ = google.from_gcloud()
    return token


TURNED_ON: set[str] = set()     # APIs this run turned on, which Google may not admit to for a minute


def gapi(method: str, url: str, body=None, missing: tuple[int, ...] = (), quota: bool = True):
    """A Google Cloud call as whoever gcloud's application default credentials are. A
    status in `missing` returns None: the thing isn't there.

    `quota` bills the call to the project, which works only for APIs turned on in it, so
    the calls that make the project and turn its APIs on go without (quota=False). An
    API this run just turned on can still be refused as off for a minute or two, so such
    a call is tried again."""
    headers = {"x-goog-user-project": GOOGLE_PROJECT} if quota else {}
    host = urllib.parse.urlsplit(url).hostname
    for attempt in range(13):
        try:
            return google.call(method, url, gcloud_token(), body=body, headers=headers, timeout=60)
        except google.GoogleError as e:
            if (quota and e.status == 403 and host in TURNED_ON and attempt < 12
                    and "has not been used in project" in str(e)):
                if not attempt:
                    log("google", f"waiting for {host}, just turned on, to answer")
                time.sleep(10)
                continue
            if e.status in missing:
                return None
            raise Fail(f"Google refused {method} {url.split('?')[0]} ({e.status}): {e}") from None


def wait_op(base: str, op: dict, what: str, quota: bool = True) -> dict:
    """Until a long-running Google operation is done; its response."""
    for _ in range(90):
        if op.get("done"):
            if op.get("error"):
                raise Fail(f"{what} failed: {op['error'].get('message')}")
            return op.get("response", {})
        time.sleep(2)
        op = gapi("GET", f"{base}/{op['name']}", quota=quota)
    raise Fail(f"timed out waiting for {what}")


def ensure_google_project(dry: bool) -> str:
    """The project that holds the service account and the pool; its number."""
    if not GOOGLE_PROJECT:
        raise Fail("add a [google] table with `project` (and `organization`, to create it) to carterlitics.toml")
    got = gapi("GET", f"{CRM}/projects/{GOOGLE_PROJECT}", missing=(403, 404), quota=False)
    if got and got.get("state") == "ACTIVE":
        log("google", f"project {GOOGLE_PROJECT} exists")
        return got["name"].split("/")[1]
    if got:
        raise Fail(f"project {GOOGLE_PROJECT} is {got.get('state')}: restore it, or name another in [google].project")
    org = str(GOOGLE_CFG.get("organization", ""))
    if dry:
        log("google", f"would create project {GOOGLE_PROJECT} in organizations/{org or '?'}")
        return NO_PROJECT
    if not org:
        raise Fail(f"no project {GOOGLE_PROJECT} that you can see: set [google].organization to create it there")
    try:
        op = google.call("POST", f"{CRM}/projects", gcloud_token(), timeout=60, body={
            "projectId": GOOGLE_PROJECT, "displayName": "carterlitics", "parent": f"organizations/{org}"})
    except google.GoogleError as e:
        if e.status == 409:
            raise Fail(f"the project id {GOOGLE_PROJECT} is taken: name another in [google].project") from None
        raise Fail(f"creating project {GOOGLE_PROJECT}: {e}") from None
    number = wait_op(CRM, op, f"creating project {GOOGLE_PROJECT}", quota=False)["name"].split("/")[1]
    log("google", f"created project {GOOGLE_PROJECT} ({number})")
    return number


def ensure_google_services(number: str, dry: bool) -> str:
    """The project number for the steps after: in a dry run with IAM still off they can't
    look inside the project, so they get NO_PROJECT and say what they would make."""
    if number == NO_PROJECT:
        log("google", "would turn on " + ", ".join(GOOGLE_SERVICES))
        return number
    got = gapi("GET", f"{USAGE}/projects/{number}/services?filter=state:ENABLED&pageSize=200", quota=False) or {}
    on = {x["config"]["name"] for x in got.get("services", [])}
    off = [x for x in GOOGLE_SERVICES if x not in on]
    if not off:
        log("google", "APIs on: " + ", ".join(x.split(".")[0] for x in GOOGLE_SERVICES))
        return number
    if dry:
        log("google", "would turn on " + ", ".join(off))
        return NO_PROJECT if "iam.googleapis.com" in off else number
    op = gapi("POST", f"{USAGE}/projects/{number}/services:batchEnable", {"serviceIds": off}, quota=False)
    wait_op(USAGE, op, "turning on " + ", ".join(off), quota=False)
    TURNED_ON.update(off)
    log("google", "turned on " + ", ".join(off))
    return number


def ensure_service_account(dry: bool) -> bool:
    """True once it exists (in a dry run, if it already did)."""
    account = CFG.google_account
    if gapi("GET", f"{IAM}/projects/{GOOGLE_PROJECT}/serviceAccounts/{account}", missing=(403, 404)):
        log("google", f"service account {account} exists")
        return True
    if dry:
        log("google", f"would create service account {account}")
        return False
    gapi("POST", f"{IAM}/projects/{GOOGLE_PROJECT}/serviceAccounts", {
        "accountId": account.split("@")[0], "serviceAccount": {
            "displayName": "carterlitics", "description": "Reads Search Console for the carterlitics collector"}})
    log("google", f"created service account {account}")
    return True


def ensure_pool(number: str, acct: str, dry: bool) -> str:
    """A workload identity pool with one provider: this AWS account, and in it only the
    function's role. The audience the function names when it signs in."""
    pool = f"projects/{number}/locations/global/workloadIdentityPools/{GOOGLE_POOL}"
    got = None if number == NO_PROJECT else gapi("GET", f"{IAM}/{pool}", missing=(404,))
    if got and got.get("state") == "DELETED":
        if dry:
            log("google", f"would restore the deleted pool {GOOGLE_POOL}")
        else:
            wait_op(IAM, gapi("POST", f"{IAM}/{pool}:undelete", {}), f"restoring pool {GOOGLE_POOL}")
            log("google", f"restored the deleted pool {GOOGLE_POOL}")
    elif got:
        log("google", f"workload identity pool {GOOGLE_POOL} exists")
    elif dry:
        log("google", f"would create workload identity pool {GOOGLE_POOL}")
    else:
        op = gapi("POST", f"{IAM}/projects/{number}/locations/global/workloadIdentityPools"
                          f"?workloadIdentityPoolId={GOOGLE_POOL}",
                  {"displayName": "carterlitics", "description": f"The {FUNCTION} function's AWS role"})
        wait_op(IAM, op, f"creating pool {GOOGLE_POOL}")
        log("google", f"created workload identity pool {GOOGLE_POOL}")
    provider = {
        "displayName": f"AWS {acct}", "aws": {"accountId": acct},
        "attributeMapping": {"google.subject": "assertion.arn",
                             "attribute.aws_role": "assertion.arn.extract('assumed-role/{role}/')"},
        "attributeCondition": f"assertion.arn.startsWith('arn:aws:sts::{acct}:assumed-role/{ROLE}/')",
    }
    path = f"{pool}/providers/{GOOGLE_PROVIDER}"
    # IAM says 403 "denied (or it may not exist)" for a provider that isn't there; were
    # it really denied, creating it below would say so.
    got = None if number == NO_PROJECT else gapi("GET", f"{IAM}/{path}", missing=(403, 404))
    if got and all(got.get(k) == v for k, v in provider.items() if k != "displayName"):
        log("google", f"provider {GOOGLE_PROVIDER}: only {ROLE} in AWS account {acct}")
    elif dry:
        log("google", f"would {'update' if got else 'create'} provider {GOOGLE_PROVIDER}: only {ROLE} in AWS account {acct}")
    elif got:
        wait_op(IAM, gapi("PATCH", f"{IAM}/{path}?updateMask=aws,attributeMapping,attributeCondition,displayName",
                          provider), f"updating provider {GOOGLE_PROVIDER}")
        log("google", f"updated provider {GOOGLE_PROVIDER}: only {ROLE} in AWS account {acct}")
    else:
        op = gapi("POST", f"{IAM}/{pool}/providers?workloadIdentityPoolProviderId={GOOGLE_PROVIDER}", provider)
        wait_op(IAM, op, f"creating provider {GOOGLE_PROVIDER}")
        log("google", f"created provider {GOOGLE_PROVIDER}: only {ROLE} in AWS account {acct}")
    return f"//iam.googleapis.com/{path}"


def ensure_account_policy(number: str, me: str, dry: bool) -> None:
    """Who may act as the service account: the function's role (through the pool), and you
    (so this script and the dev server can)."""
    account = CFG.google_account
    url = f"{IAM}/projects/{GOOGLE_PROJECT}/serviceAccounts/{account}"
    wanted = {
        "roles/iam.workloadIdentityUser":
            f"principalSet://iam.googleapis.com/projects/{number}/locations/global/workloadIdentityPools/"
            f"{GOOGLE_POOL}/attribute.aws_role/{ROLE}",
        "roles/iam.serviceAccountTokenCreator": f"user:{me}",
    }
    policy = {} if number == NO_PROJECT else gapi("POST", f"{url}:getIamPolicy", {}, missing=(403, 404)) or {}
    bindings = policy.setdefault("bindings", [])
    changed = []
    for role, member in wanted.items():
        b = next((b for b in bindings if b["role"] == role and not b.get("condition")), None)
        if b is None:
            b = {"role": role, "members": []}
            bindings.append(b)
        if member not in b["members"]:
            b["members"].append(member)
            changed.append(f"{member.split('/')[-1] if member.startswith('principalSet') else member} as {role}")
    if not changed:
        log("google", f"{ROLE} (through the pool) and {me} may act as the service account")
    elif dry:
        log("google", "would let " + "; ".join(changed))
    else:
        gapi("POST", f"{url}:setIamPolicy", {"policy": policy})
        log("google", "let " + "; ".join(changed))


def zones_for_sites() -> dict[str, str]:
    """Route 53 zones the sites' hosts are under: domain -> zone id. Those are the
    domains whose DNS we hold, so the ones the service account can verify."""
    zones = {z["Name"].rstrip(".").lower(): z["Id"].split("/")[-1]
             for z in aws("route53", "list-hosted-zones")["HostedZones"] if not z["Config"].get("PrivateZone")}
    found = {}
    for site in CFG.sites.values():
        for host in google.hosts_of(site):
            under = [d for d in zones if host == d or host.endswith("." + d)]
            if under:
                d = max(under, key=len)
                found[d] = zones[d]
    return dict(sorted(found.items()))


def add_txt(domain: str, zone: str, value: str, dry: bool) -> bool:
    """Add a TXT value at the domain's apex, keeping the values already there. True if it
    had to be added."""
    sets = aws("route53", "list-resource-record-sets", "--hosted-zone-id", zone, "--start-record-name", domain,
               "--start-record-type", "TXT", "--max-items", "1")["ResourceRecordSets"]
    current = next((r for r in sets if r["Name"].rstrip(".").lower() == domain and r["Type"] == "TXT"), None)
    values = [r["Value"] for r in current["ResourceRecords"]] if current else []
    quoted = json.dumps(value)
    if quoted in values:
        return False
    if dry:
        log("dns", f"would add TXT {quoted} at {domain}, beside its {len(values)} other value(s)")
        return True
    change = {"Comment": "carterlitics: Search Console verification for its service account", "Changes": [{
        "Action": "UPSERT", "ResourceRecordSet": {
            "Name": f"{domain}.", "Type": "TXT", "TTL": current["TTL"] if current else 300,
            "ResourceRecords": [{"Value": v} for v in values + [quoted]]}}]}
    got = aws("route53", "change-resource-record-sets", "--hosted-zone-id", zone, "--change-batch", json.dumps(change))
    aws("route53", "wait", "resource-record-sets-changed", "--id", got["ChangeInfo"]["Id"])
    log("dns", f"added TXT {quoted} at {domain}, beside its {len(values)} other value(s)")
    return True


def as_account(tokens: google.Tokens, method: str, url: str, scope: str, body=None) -> dict:
    """A call as the service account. Being allowed to act as it can take a minute to
    reach every Google server after the grant, and so can an API just turned on, so
    those refusals are retried for a while."""
    for attempt in range(13):
        try:
            return tokens.request(method, url, scope, body)
        except google.GoogleError as e:
            waiting = e.status == 403 and attempt < 12 and (
                "getAccessToken" in str(e) or (TURNED_ON and "has not been used in project" in str(e)))
            if not waiting:
                raise Fail(f"as {tokens.account}, {method} {url.split('?')[0]}: {e}") from None
            if attempt == 0:
                log("google", "waiting for the grant and the APIs to take effect…")
            time.sleep(10)
    raise AssertionError


def verify_domains(tokens: google.Tokens, dry: bool) -> None:
    """Each domain: the service account a verified owner (DNS TXT), and its domain
    property in the account's Search Console. Changes DNS, so only with --verify-dns."""
    zones = zones_for_sites()
    if not zones:
        log("verify", "no site is under a Route 53 zone of this account: nothing to verify")
        return
    owned = {urllib.parse.unquote(r["id"]) for r in
             as_account(tokens, "GET", f"{SITE_VERIFICATION}/webResource", google.VERIFY).get("items", [])}
    properties = {e["siteUrl"]: e.get("permissionLevel") for e in
                  as_account(tokens, "GET", f"{google.API}/sites", google.READ).get("siteEntry", [])}
    for domain, zone in zones.items():
        site = {"type": "INET_DOMAIN", "identifier": domain}
        if f"dns://{domain}" in owned:
            log("verify", f"{domain}: the service account is a verified owner")
        else:
            token = as_account(tokens, "POST", f"{SITE_VERIFICATION}/token", google.VERIFY,
                               {"site": site, "verificationMethod": "DNS_TXT"})["token"]
            add_txt(domain, zone, token, dry)
            if dry:
                log("verify", f"would verify {domain} through that record")
            else:
                verify_one(tokens, domain, site)
        prop = f"sc-domain:{domain}"
        if properties.get(prop) in ("siteOwner", "siteFullUser", "siteRestrictedUser"):
            log("search", f"{prop}: in the account's Search Console ({properties[prop]})")
        elif dry:
            log("search", f"would add {prop} to the account's Search Console")
        else:
            as_account(tokens, "PUT", f"{google.API}/sites/{urllib.parse.quote(prop, safe='')}", google.WRITE)
            log("search", f"added {prop} to the account's Search Console")


def verify_one(tokens: google.Tokens, domain: str, site: dict) -> None:
    """Google reads the record through its own resolvers, which may still hold the zone's
    old answer for its TTL: try for six minutes."""
    for attempt in range(25):
        try:
            tokens.request("POST", f"{SITE_VERIFICATION}/webResource?verificationMethod=DNS_TXT", google.VERIFY,
                           {"site": site})
            log("verify", f"{domain}: verified; the service account is an owner")
            return
        except google.GoogleError as e:
            if e.status != 400 or attempt == 24:
                raise Fail(f"verifying {domain}: {e}. The TXT record stays; re-run `search-console` later.") from None
            if attempt == 0:
                log("verify", f"{domain}: waiting for Google to see the new TXT record…")
            time.sleep(15)


def show_properties(tokens: google.Tokens) -> None:
    """What the account reads, which property each site's numbers come from, and, for the
    sites none covers, how to give it one."""
    got = as_account(tokens, "GET", f"{google.API}/sites", google.READ).get("siteEntry", [])
    props = sorted(e["siteUrl"] for e in got if e.get("permissionLevel") != "siteUnverifiedUser")
    log("search", f"{tokens.account} reads {len(props)} propert{'y' if len(props) == 1 else 'ies'}: "
        + (", ".join(f"{e['siteUrl']} ({e.get('permissionLevel')})" for e in got if e["siteUrl"] in props)
           or "none"))
    for site in CFG.sites.values():
        hosts = google.hosts_of(site)
        if not hosts:
            continue
        covered = google.plan(site, props)
        missing = [h for h in hosts if not any(h in hs for _, hs in covered)]
        log("site", f"{site.id:<26} " + ("; ".join(f"{p} ({', '.join(hs)})" for p, hs in covered) or "no property")
            + (f"; none for {', '.join(missing)}" if missing and covered else ""))
    uncovered = [d for d in zones_for_sites() if not google.property_for(d, props)]
    for domain in uncovered:
        log("todo", f"sc-domain:{domain}: in Search Console, Settings > Users and permissions > Add user: "
            f"{tokens.account}, Restricted (read only). Or re-run with --verify-dns to verify it "
            "through a TXT record (owner)")


def check_function() -> None:
    """Ask the deployed function which properties it can read, as itself."""
    env = (aws("lambda", "get-function-configuration", "--function-name", FUNCTION,
               missing=("ResourceNotFoundException",)) or {}).get("Environment", {}).get("Variables", {})
    if not env.get("GOOGLE_AUDIENCE"):
        log("function", f"{FUNCTION} doesn't know the pool yet: run `infra/deploy.py provision`")
        return
    out = Path(tempfile.mkdtemp()) / "out.json"
    aws("lambda", "invoke", "--function-name", FUNCTION, "--cli-binary-format", "raw-in-base64-out",
        "--payload", json.dumps({"task": "search-console"}), str(out))
    got = json.loads(out.read_text() or "{}")
    if got.get("ok") and got["properties"]:
        log("function", f"{FUNCTION} signs in as {got['account']} and reads " +
            ", ".join(p["site_url"] for p in got["properties"]))
    elif got.get("ok"):
        log("function", f"{FUNCTION} signs in as {got['account']} but reads no properties: "
            "give it access to each one (README: Search Console)")
    else:
        raise Fail(f"{FUNCTION} can't read Search Console: {got.get('error') or got.get('errorMessage') or got}")


def search_console(acct: str, dry: bool, verify_dns: bool) -> None:
    if not CFG.google_account:
        raise Fail("add a [google] table with `project` to carterlitics.toml")
    me = google.call("GET", f"https://oauth2.googleapis.com/tokeninfo?access_token={gcloud_token()}").get("email", "")
    if not me:
        raise Fail("gcloud's application default credentials name no user (a service account can't run this)")
    log("google", f"as {me}")
    number = ensure_google_services(ensure_google_project(dry), dry)
    exists = ensure_service_account(dry)
    audience = ensure_pool(number, acct, dry)
    ensure_account_policy(number, me, dry)
    if param("google-audience") == audience:
        log("secret", f"{PARAMS}/google-audience is the provider")
    elif dry:
        log("secret", f"would set {PARAMS}/google-audience to {audience}")
    else:
        put_param("google-audience", audience)
        log("secret", f"set {PARAMS}/google-audience: `provision` hands it to the function")
    if not exists:
        log("search", "the service account doesn't exist yet, so it reads nothing to list")
        return
    tokens = google.Tokens(CFG.google_account, google.from_gcloud)
    if verify_dns:
        verify_domains(tokens, dry)
    show_properties(tokens)
    if not dry:
        check_function()


MAXMIND = "https://download.maxmind.com/geoip/databases/{}/download?suffix=tar.gz"


def fetch_geo() -> None:
    """GeoLite2 City and ASN from MaxMind (free account), else IPinfo Lite."""
    account, key = os.environ.get("MAXMIND_ACCOUNT_ID"), os.environ.get("MAXMIND_LICENSE_KEY")
    token = os.environ.get("IPINFO_TOKEN")
    if not (account and key) and not token:
        raise Fail("set MAXMIND_ACCOUNT_ID and MAXMIND_LICENSE_KEY (free at maxmind.com), or IPINFO_TOKEN "
                   "(free at ipinfo.io), or put any .mmdb file in geo/ yourself")
    GEO_DIR.mkdir(exist_ok=True)
    if account and key:
        auth = base64.b64encode(f"{account}:{key}".encode()).decode()
        for edition in ("GeoLite2-City", "GeoLite2-ASN"):
            req = urllib.request.Request(MAXMIND.format(edition), headers={"Authorization": f"Basic {auth}"})
            try:
                with urllib.request.urlopen(req, timeout=300) as res:
                    archive = res.read()
            except urllib.error.HTTPError as e:
                raise Fail(f"MaxMind {edition}: HTTP {e.code} (check the account id and license key)") from None
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
                member = next((m for m in tar.getmembers() if m.isfile() and m.name.endswith(f"/{edition}.mmdb")), None)
                if member is None:
                    raise Fail(f"MaxMind {edition}: no .mmdb in the archive")
                target = GEO_DIR / f"{edition}.mmdb"
                target.write_bytes(tar.extractfile(member).read())
            log("geo", f"{target.relative_to(ROOT)}: {target.stat().st_size // (1024 * 1024)} MB")
        if (GEO_DIR / "GeoLite2-Country.mmdb").exists():
            log("geo", "geo/GeoLite2-Country.mmdb is now redundant (City has countries); delete it to "
                "shrink the package")
    else:
        target = GEO_DIR / "ipinfo_lite.mmdb"
        with urllib.request.urlopen(f"https://ipinfo.io/data/ipinfo_lite.mmdb?token={token}", timeout=120) as res:
            target.write_bytes(res.read())
        log("geo", f"{target.relative_to(ROOT)}: {target.stat().st_size // (1024 * 1024)} MB")
    log("geo", "`provision` points the function at it, `code` ships it")


# ---------------------------------------------------------------- published ranges

FETCH_AGENT = "carterlitics-deploy (+https://cartercole.net)"
GOOGLE_RANGES = "https://developers.google.com/static/crawling/ipranges/"


def _prefixes(body: bytes) -> list[str]:
    """The {"prefixes": [{"ipv4Prefix" | "ipv6Prefix": …}]} shape Google, Bing, OpenAI,
    Perplexity, Apple, DuckDuckGo and Common Crawl all publish."""
    return [p.get("ipv4Prefix") or p.get("ipv6Prefix") for p in json.loads(body).get("prefixes", [])
            if isinstance(p, dict) and (p.get("ipv4Prefix") or p.get("ipv6Prefix"))]


def _lines(body: bytes) -> list[str]:
    """One range per line (the first field, split on space, tab or comma); # comments."""
    out = []
    for line in body.decode("utf-8", "replace").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(re.split(r"[\s,;]+", line)[0])
    return out


def tagged(tag: str, parse):
    return lambda body: [(tag, r) for r in parse(body)]


def _aws(body: bytes):
    data = json.loads(body)
    for p in data.get("prefixes", []) + data.get("ipv6_prefixes", []):
        cidr = p.get("ip_prefix") or p.get("ipv6_prefix")
        yield "cloud:AWS", cidr
        if p.get("service") == "EC2":
            yield f"cloud:AWS EC2 {p.get('region')}", cidr
        elif p.get("service") == "CLOUDFRONT":
            yield "cloud:AWS CloudFront", cidr


def _gcp(body: bytes):
    for p in json.loads(body).get("prefixes", []):
        yield f"cloud:Google Cloud {p.get('scope', '')}".strip(), p.get("ipv4Prefix") or p.get("ipv6Prefix")


def _azure(page: bytes):
    """The download page names this week's JSON file; fetch that."""
    found = re.search(rb"https://download\.microsoft\.com/download/[^\"']+?ServiceTags_Public_\d+\.json", page)
    if not found:
        raise ValueError("no ServiceTags link on the download page")
    for v in json.loads(fetch(found.group(0).decode())).get("values", []):
        name = v.get("name", "")
        if name.startswith("AzureCloud."):
            for cidr in v.get("properties", {}).get("addressPrefixes", []):
                yield f"cloud:Azure {name.split('.', 1)[1]}", cidr


def _oracle(body: bytes):
    for region in json.loads(body).get("regions", []):
        for c in region.get("cidrs", []):
            yield f"cloud:Oracle Cloud {region.get('region', '')}".strip(), c.get("cidr")


def _mullvad(body: bytes):
    for relay in json.loads(body):
        if relay.get("type") in ("wireguard", "openvpn"):
            for k in ("ipv4_addr_in", "ipv6_addr_in"):
                if relay.get(k):
                    yield "vpn:Mullvad", relay[k]


def _dshield(body: bytes):
    for line in body.decode("utf-8", "replace").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and not line.startswith("#") and parts[2].strip().isdigit():
            yield "abuse:DShield", f"{parts[0].strip()}/{parts[2].strip()}"


def _json_lines(tag: str, key: str):
    def parse(body: bytes):
        for line in body.decode("utf-8", "replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get(key):
                yield tag, row[key]
    return parse


# (what, URL, parser -> (tag, range) pairs). Tags are `<kind>:<name>`; carterlitics/intel.py
# says what each kind means, and its PROOFS name the crawler lists it checks.
INTEL_SOURCES = [
    ("Googlebot", GOOGLE_RANGES + "common-crawlers.json", tagged("crawler:Googlebot", _prefixes)),
    ("Google special crawlers", GOOGLE_RANGES + "special-crawlers.json",
     tagged("crawler:Google special crawlers", _prefixes)),
    ("Google fetchers", GOOGLE_RANGES + "user-triggered-fetchers.json", tagged("crawler:Google fetchers", _prefixes)),
    ("Google fetchers (Google)", GOOGLE_RANGES + "user-triggered-fetchers-google.json",
     tagged("crawler:Google fetchers", _prefixes)),
    ("Bingbot", "https://www.bing.com/toolbox/bingbot.json", tagged("crawler:Bingbot", _prefixes)),
    ("GPTBot", "https://openai.com/gptbot.json", tagged("crawler:GPTBot", _prefixes)),
    ("OAI-SearchBot", "https://openai.com/searchbot.json", tagged("crawler:OAI-SearchBot", _prefixes)),
    ("ChatGPT-User", "https://openai.com/chatgpt-user.json", tagged("crawler:ChatGPT-User", _prefixes)),
    ("PerplexityBot", "https://www.perplexity.com/perplexitybot.json", tagged("crawler:PerplexityBot", _prefixes)),
    ("Perplexity-User", "https://www.perplexity.com/perplexity-user.json", tagged("crawler:Perplexity-User", _prefixes)),
    ("Applebot", "https://search.developer.apple.com/applebot.json", tagged("crawler:Applebot", _prefixes)),
    ("DuckDuckBot", "https://duckduckgo.com/duckduckbot.json", tagged("crawler:DuckDuckBot", _prefixes)),
    ("DuckAssistBot", "https://duckduckgo.com/duckassistbot.json", tagged("crawler:DuckAssistBot", _prefixes)),
    ("CCBot", "https://index.commoncrawl.org/ccbot.json", tagged("crawler:CCBot", _prefixes)),
    ("UptimeRobot", "https://uptimerobot.com/inc/files/ips/IPv4andIPv6.txt", tagged("crawler:UptimeRobot", _lines)),
    ("Pingdom", "https://my.pingdom.com/probes/ipv4", tagged("crawler:Pingdom", _lines)),
    ("AWS", "https://ip-ranges.amazonaws.com/ip-ranges.json", _aws),
    ("Google Cloud", "https://www.gstatic.com/ipranges/cloud.json", _gcp),
    ("Azure", "https://www.microsoft.com/en-us/download/details.aspx?id=56519", _azure),
    ("Oracle Cloud", "https://docs.oracle.com/en-us/iaas/tools/public_ip_ranges.json", _oracle),
    ("Cloudflare v4", "https://www.cloudflare.com/ips-v4", tagged("cloud:Cloudflare", _lines)),
    ("Cloudflare v6", "https://www.cloudflare.com/ips-v6", tagged("cloud:Cloudflare", _lines)),
    ("Fastly", "https://api.fastly.com/public-ip-list",
     lambda b: [("cloud:Fastly", c) for k in ("addresses", "ipv6_addresses") for c in json.loads(b).get(k, [])]),
    ("DigitalOcean", "https://digitalocean.com/geo/google.csv", tagged("cloud:DigitalOcean", _lines)),
    ("Linode", "https://geoip.linode.com/", tagged("cloud:Linode", _lines)),
    ("Vultr", "https://geofeed.constant.com/?json",
     lambda b: [("cloud:Vultr", s["ip_prefix"]) for s in json.loads(b).get("subnets", []) if s.get("ip_prefix")]),
    ("GitHub Actions", "https://api.github.com/meta",
     lambda b: [("cloud:GitHub Actions", c) for c in json.loads(b).get("actions", [])]),
    ("Datacenters (X4BNet)", "https://raw.githubusercontent.com/X4BNet/lists_vpn/main/output/datacenter/ipv4.txt",
     tagged("hosting:datacenter", _lines)),
    ("VPN providers (X4BNet)", "https://raw.githubusercontent.com/X4BNet/lists_vpn/main/output/vpn/ipv4.txt",
     tagged("vpn:VPN provider", _lines)),
    ("Mullvad", "https://api.mullvad.net/www/relays/all/", _mullvad),
    ("Tor exits", "https://check.torproject.org/torbulkexitlist", tagged("tor:Tor exit", _lines)),
    ("iCloud Private Relay", "https://mask-api.icloud.com/egress-ip-ranges.csv",
     tagged("relay:iCloud Private Relay", _lines)),
    ("Starlink", "https://geoip.starlinkisp.net/feed.csv", tagged("isp:Starlink", _lines)),
    ("Spamhaus DROP v4", "https://www.spamhaus.org/drop/drop_v4.json", _json_lines("drop:Spamhaus DROP", "cidr")),
    ("Spamhaus DROP v6", "https://www.spamhaus.org/drop/drop_v6.json", _json_lines("drop:Spamhaus DROP", "cidr")),
    ("blocklist.de", "https://lists.blocklist.de/lists/all.txt", tagged("abuse:blocklist.de", _lines)),
    ("DShield", "https://feeds.dshield.org/block.txt", _dshield),
]


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": FETCH_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=120) as res:
        return res.read()


def fetch_intel() -> None:
    """Every list in INTEL_SOURCES, into one table. A list that fails is left out (and
    said so); the table records what it was built from."""
    from concurrent.futures import ThreadPoolExecutor

    def one(source):
        what, url, parse = source
        try:
            return what, list(parse(fetch(url))), ""
        except Exception as e:  # noqa: BLE001 - one list down shouldn't stop the rest
            return what, [], f"{type(e).__name__}: {str(e)[:160]}"

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(one, INTEL_SOURCES))
    entries, notes = [], {}
    for what, pairs, error in results:
        pairs = [(t, r) for t, r in pairs if t and r]
        entries += pairs
        notes[what] = error or len(pairs)
        log("intel", f"{what:<26} {error or f'{len(pairs):,} ranges'}")
    if not entries:
        raise Fail("no list could be fetched")
    built = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    blob = build_ranges(entries, built, notes)
    GEO_DIR.mkdir(exist_ok=True)
    RANGES.write_bytes(blob)
    table = Ranges(blob)
    log("intel", f"{RANGES.relative_to(ROOT)}: {len(blob) / 1e6:.1f} MB, {table.n4:,} IPv4 and {table.n6:,} IPv6 "
        f"pieces, {len(table.combos):,} tag sets. `provision` points the function at it, `code` ships it")
    failed = [w for w, n in notes.items() if isinstance(n, str)]
    if failed:
        log("intel", f"left out: {', '.join(failed)}")


# ---------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["code", "provision", "routes", "logging", "register-client",
                                        "server-key", "grant-deploy", "search-console", "geo", "intel"])
    ap.add_argument("site", nargs="?", help="routes, logging: a site id from carterlitics.toml, or all; server-key: a site")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rollback", action="store_true", help="routes: restore the config from before")
    ap.add_argument("--drop", nargs="*", default=[], help="routes: behaviors to remove, e.g. '/api/*'")
    ap.add_argument("--force", action="store_true", help="register-client: register again")
    ap.add_argument("--verify-dns", action="store_true",
                    help="search-console: verify each site's domain through a TXT record in Route 53")
    args = ap.parse_args()
    try:
        if args.command in ("routes", "logging", "server-key") and not args.site:
            raise Fail(f"{args.command} needs a site: {', '.join(CFG.sites)}")
        if args.command not in ("code", "geo", "intel"):
            require_admin(args.command)
        if args.command == "code":
            ship_code(args.dry_run)
        elif args.command == "provision":
            provision(args.dry_run)
        elif args.command == "routes":
            routes(args.site, args.dry_run, args.rollback, args.drop)
        elif args.command == "logging":
            logging(args.site, args.dry_run)
        elif args.command == "server-key":
            print_server_key(args.site)
        elif args.command == "grant-deploy":
            grant_deploy(account(), args.dry_run)
        elif args.command == "search-console":
            search_console(account(), args.dry_run, args.verify_dns)
        elif args.command == "geo":
            fetch_geo()
        elif args.command == "intel":
            fetch_intel()
        else:
            register_client(args.force)
    except Fail as e:
        print(f"deploy: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
