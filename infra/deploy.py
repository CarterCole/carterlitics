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
    infra/deploy.py geo                       fetch IPinfo Lite into geo/ (needs IPINFO_TOKEN)

Settings are carterlitics.toml's [aws] table and its site list. Every step looks before it
creates, so re-running is safe. It drives the AWS CLI (no boto3) with whatever credentials
the environment selects: AWS_PROFILE=carter-admin for everything but `code`.

Secrets live in SSM Parameter Store under [aws].params and reach the function as
environment variables. They go to the CLI in files readable only by you, never on a
command line.
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
import secrets
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PACKAGE = ROOT / "carterlitics"
GEO_DIR = ROOT / "geo"
STATE = HERE / ".state"
CONFIG = ROOT / "carterlitics.toml"

sys.path.insert(0, str(ROOT))
from carterlitics.auth import server_key  # noqa: E402
from carterlitics.config import Config  # noqa: E402

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
ORIGIN_REQUEST_POLICY = AWS.get("origin_request_policy", "carterlitics")
LOG_DAYS = 7          # raw access log files: counted within minutes, kept a week for re-runs
DIRECT_UPLOAD = 45 * 1024 * 1024

# Managed CloudFront cache policies.
CACHING_DISABLED = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
CACHING_OPTIMIZED = "658327ea-f89d-4fab-a63d-7e88639e58f6"

# What the collector reads off a request. Host is left out on purpose: a Function URL
# answers only to its own. The CloudFront-* ones are set by CloudFront, not the client.
FORWARDED_HEADERS = [
    "origin", "user-agent", "sec-gpc", "dnt", "x-carterlitics-key",
    "cloudfront-viewer-address", "cloudfront-viewer-country", "cloudfront-viewer-asn", "cloudfront-viewer-city",
]
ORIGIN_ID = "carterlitics"
# The access log fields the collector reads (no cookies: they never leave CloudFront).
LOG_FIELDS = ["timestamp(ms)", "c-ip", "cs-method", "x-host-header", "cs-uri-stem", "cs-uri-query", "sc-status",
              "cs(Referer)", "cs(User-Agent)", "sc-bytes", "x-edge-result-type", "time-taken", "c-country", "asn"]


class Fail(Exception):
    pass


def log(step: str, msg: str) -> None:
    print(f"  {step:<9} {msg}", flush=True)


def aws(*args: str, check: bool = True, payload: dict | None = None):
    """Run the CLI and parse its JSON. `payload` goes in via --cli-input-json from a file
    readable only by us, which is how secrets travel."""
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
        if not check:
            return None
        raise Fail(f"aws {' '.join(args[:2])} failed:\n{proc.stderr.strip()}")
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def error_of(*args: str) -> str:
    proc = subprocess.run(["aws", *args, "--region", REGION, "--output", "json"], capture_output=True, text=True)
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
    """The collector and its static files, plus any .mmdb in geo/. Byte-identical for
    identical inputs, so CodeSha256 says whether Lambda already runs it."""
    files = sorted(p for p in PACKAGE.rglob("*") if p.is_file() and "__pycache__" not in p.parts
                   and not p.name.startswith("."))
    files += sorted(GEO_DIR.glob("*.mmdb")) if GEO_DIR.exists() else []
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
    got = aws("ssm", "get-parameter", "--name", f"{PARAMS}/{name}", "--with-decryption", check=False)
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
    got = aws("dynamodb", "describe-table", "--table-name", TABLE, check=False)
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
        raise Fail(f"s3://{name} belongs to another account; pick another name in carterlitics.toml")
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
    got = aws("iam", "get-role", "--role-name", ROLE, check=False)
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
                                       "dynamodb:Query", "dynamodb:BatchWriteItem"], "Resource": table_arn},
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
        "ORIGIN_SECRET": ensure_secret("origin-secret", dry),
        "SESSION_SECRET": ensure_secret("session-secret", dry),
        "SERVER_SECRET": ensure_secret("server-secret", dry),
        "OIDC_CLIENT_ID": client_id or "",
        "OIDC_CLIENT_SECRET": client_secret or "",
        "ADMIN_IDS": param("admin-ids") or "",
    }
    size = sum(len(k) + len(v) for k, v in env.items())
    if size > 4000:
        raise Fail(f"environment is {size} bytes; Lambda allows 4 KB. Trim carterlitics.toml.")
    return env


def ensure_function(role_arn: str, env: dict, dry: bool) -> str:
    config = {
        "FunctionName": FUNCTION, "Role": role_arn, "Runtime": "python3.13",
        "Handler": "carterlitics.app.lambda_handler", "MemorySize": 512, "Timeout": 60,
        "Environment": {"Variables": env},
        "Description": "carterlitics: first-party analytics for " + ", ".join(CFG.sites),
        "LoggingConfig": {"LogFormat": "Text", "LogGroup": LOG_GROUP},
    }
    exists = not error_of("lambda", "get-function-configuration", "--function-name", FUNCTION)
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
            if aws("lambda", "create-function", payload=create, check=attempt == 11) is not None:
                break
            time.sleep(5)
        wait_updated()
        log("function", f"created {FUNCTION}")
    return aws("lambda", "get-function-configuration", "--function-name", FUNCTION)["FunctionArn"]


def ensure_url(dry: bool) -> str:
    got = aws("lambda", "get-function-url-config", "--function-name", FUNCTION, check=False)
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


def ensure_origin_request_policy(dry: bool) -> str:
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
            if policy["OriginRequestPolicyConfig"] != config and not dry:
                etag = aws("cloudfront", "get-origin-request-policy", "--id", policy["Id"])["ETag"]
                aws("cloudfront", "update-origin-request-policy", "--id", policy["Id"], "--if-match", etag,
                    "--origin-request-policy-config", json.dumps(config))
                log("policy", f"updated origin request policy {policy['Id']}")
            else:
                log("policy", f"origin request policy {policy['Id']}")
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
    origins = [o for o in new["Origins"].get("Items", []) if o["Id"] != ORIGIN_ID] + [origin]
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
    paths = {b["PathPattern"] for b in ours}
    rest = [b for b in new.get("CacheBehaviors", {}).get("Items", [])
            if b["PathPattern"] not in paths and b["PathPattern"] not in drop]
    behaviors = ours + rest
    new["CacheBehaviors"] = {"Quantity": len(behaviors), "Items": behaviors}
    return new


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
    for site in targets(name, clients_only=True):
        for dist in distributions(site):
            log("cdn", f"{site.id}: distribution {dist}")
            route_one(site, dist, dry, rollback, drop)


def route_one(site, dist: str, dry: bool, rollback: bool, drop: list[str]) -> None:
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
        aws("cloudfront", "update-distribution", "--id", dist, "--if-match", etag,
            "--distribution-config", json.dumps(before))
        log("cdn", f"{dist} restored from {backup.name}; deploying (a few minutes)")
        return

    url = aws("lambda", "get-function-url-config", "--function-name", FUNCTION)["FunctionUrl"]
    api_host = url.split("//", 1)[1].rstrip("/")
    secret = param("origin-secret")
    if not secret:
        raise Fail("no origin secret: run `infra/deploy.py provision` first")
    policy_id = ensure_origin_request_policy(dry)
    new = routed(live, site, api_host, secret, policy_id, drop)
    if not show_diff(live, new) or dry:
        return
    # Keep the first backup: a later apply would otherwise replace the way back.
    if not backup.exists():
        STATE.mkdir(exist_ok=True)
        backup.write_text(json.dumps(live, indent=2))
        os.chmod(backup, 0o600)
    aws("cloudfront", "update-distribution", "--id", dist, "--if-match", etag,
        "--distribution-config", json.dumps(new))
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
        "--s3-delivery-configuration", "suffixPath={DistributionId}/{yyyy}/{MM}/{dd}/{HH},enableHiveCompatiblePath=false")
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


def fetch_geo() -> None:
    token = os.environ.get("IPINFO_TOKEN")
    if not token:
        raise Fail("set IPINFO_TOKEN (free at ipinfo.io), or put any .mmdb file in geo/ yourself")
    GEO_DIR.mkdir(exist_ok=True)
    target = GEO_DIR / "ipinfo_lite.mmdb"
    with urllib.request.urlopen(f"https://ipinfo.io/data/ipinfo_lite.mmdb?token={token}", timeout=120) as res:
        target.write_bytes(res.read())
    log("geo", f"{target.relative_to(ROOT)}: {target.stat().st_size // (1024 * 1024)} MB; "
        "`provision` points the function at it, `code` ships it")


# ---------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["code", "provision", "routes", "logging", "register-client",
                                        "server-key", "grant-deploy", "geo"])
    ap.add_argument("site", nargs="?", help="routes, logging: a site id from carterlitics.toml, or all; server-key: a site")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rollback", action="store_true", help="routes: restore the config from before")
    ap.add_argument("--drop", nargs="*", default=[], help="routes: behaviors to remove, e.g. '/api/*'")
    ap.add_argument("--force", action="store_true", help="register-client: register again")
    args = ap.parse_args()
    try:
        if args.command in ("routes", "logging", "server-key") and not args.site:
            raise Fail(f"{args.command} needs a site: {', '.join(CFG.sites)}")
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
        elif args.command == "geo":
            fetch_geo()
        else:
            register_client(args.force)
    except Fail as e:
        print(f"deploy: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
