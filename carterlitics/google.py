"""Google Search Console: what Google's search shows of each site, read by a service account.

The collector keeps no Google key. On Lambda it signs in to Google as its own AWS role,
through workload identity federation (GOOGLE_AUDIENCE names the pool's provider), and
then acts as the service account `[google]` in carterlitics.toml names. On a laptop,
gcloud's application default credentials act as the same account; `infra/deploy.py
search-console` lets the person who ran it do that. The account is a verified owner of
each domain the sites live under, verified through a DNS TXT record, so nobody has to
add it to a property by hand.

    sc-domain:cartercole.net    every host under the domain: cartercole.net, mosaic.cartercole.net, …
    https://example.com/        a URL-prefix property, used for a host it covers when there is one

A site's report is its hosts' share of the property (a page filter), so mosaic's
numbers are mosaic's own though the property covers the whole domain. A site whose
hosts sit under two domains (resume.cartercole.net and .com) adds the two together.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

READ = "https://www.googleapis.com/auth/webmasters.readonly"
WRITE = "https://www.googleapis.com/auth/webmasters"
VERIFY = "https://www.googleapis.com/auth/siteverification"
CLOUD = "https://www.googleapis.com/auth/cloud-platform"

API = "https://searchconsole.googleapis.com/webmasters/v3"
STS = "https://sts.googleapis.com/v1/token"
IAM_CREDENTIALS = "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts"

REPORT_TTL = 15 * 60       # Google refreshes its numbers a few times a day
PROPERTIES_TTL = 10 * 60
ROWS = 1000                # per dimension and property: more than a personal site has
DIMENSIONS = ("query", "page", "country", "device")


class GoogleError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def call(method: str, url: str, token: str = "", body=None, form: dict | None = None,
         timeout: float = 20, headers: dict | None = None) -> dict:
    """One request to a Google API; its JSON answer, or GoogleError with Google's message."""
    headers = {"Accept": "application/json", "User-Agent": "carterlitics", **(headers or {})}
    data = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read()
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            err = json.loads(raw)
            detail = err.get("error")
            message = (detail.get("message") if isinstance(detail, dict) else
                       err.get("error_description") or detail) or raw.decode()[:300]
        except ValueError:
            message = raw.decode(errors="replace")[:300] or e.reason
        raise GoogleError(e.code, str(message)) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise GoogleError(0, f"couldn't reach {urllib.parse.urlsplit(url).hostname}: {e}") from None
    return json.loads(raw) if raw.strip() else {}


# ---------------------------------------------------------------- signing in


def sigv4(method: str, host: str, query: str, headers: dict, region: str, service: str,
          key: str, secret: str, stamp: str, payload: bytes = b"") -> str:
    """The Authorization header AWS Signature Version 4 gives a request to https://host/?query.
    `headers` (lower-case names, host and x-amz-date among them) are the ones signed;
    `query` is already canonical (sorted, encoded)."""
    names = sorted(headers)
    canonical = "\n".join([method, "/", query, "".join(f"{n}:{headers[n]}\n" for n in names),
                           ";".join(names), hashlib.sha256(payload).hexdigest()])
    scope = f"{stamp[:8]}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    k = f"AWS4{secret}".encode()
    for part in (stamp[:8], region, service, "aws4_request"):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    return f"AWS4-HMAC-SHA256 Credential={key}/{scope}, SignedHeaders={';'.join(names)}, Signature={signature}"


def aws_subject_token(audience: str, region: str, key: str, secret: str, session_token: str = "",
                      now: datetime | None = None) -> str:
    """A signed AWS STS GetCallerIdentity request, serialized the way Google's STS takes it
    as proof of an AWS identity. Google sends it to AWS itself; the request says who we
    are and, signed into it, which provider it is for (x-goog-cloud-target-resource)."""
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    host = f"sts.{region}.amazonaws.com"
    query = "Action=GetCallerIdentity&Version=2011-06-15"
    headers = {"host": host, "x-amz-date": stamp, "x-goog-cloud-target-resource": audience}
    if session_token:
        headers["x-amz-security-token"] = session_token
    headers["Authorization"] = sigv4("POST", host, query, headers, region, "sts", key, secret, stamp)
    request = {"url": f"https://{host}?{query}", "method": "POST",
               "headers": [{"key": k, "value": headers[k]} for k in sorted(headers)]}
    return urllib.parse.quote(json.dumps(request, separators=(",", ":"), sort_keys=True))


def from_aws(audience: str, env=os.environ, fetch=call):
    """A token source for Lambda: its role's credentials, exchanged at Google's STS."""
    def source() -> tuple[str, float]:
        subject = aws_subject_token(audience, env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or "us-east-1",
                                    env["AWS_ACCESS_KEY_ID"], env["AWS_SECRET_ACCESS_KEY"],
                                    env.get("AWS_SESSION_TOKEN", ""))
        got = fetch("POST", STS, form={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "audience": audience, "scope": CLOUD,
            "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "subject_token_type": "urn:ietf:params:aws:token-type:aws4_request",
            "subject_token": subject})
        return got["access_token"], time.time() + int(got.get("expires_in", 3600))
    return source


def from_gcloud() -> tuple[str, float]:
    """A token source for a laptop: gcloud's application default credentials."""
    try:
        proc = subprocess.run(["gcloud", "auth", "application-default", "print-access-token"],
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GoogleError(0, f"gcloud isn't available to sign in to Google ({e})") from None
    if proc.returncode != 0:
        last = (proc.stderr.strip().splitlines() or ["no output"])[-1]
        raise GoogleError(401, f"gcloud has no application default credentials: run "
                               f"`gcloud auth application-default login` ({last})")
    return proc.stdout.strip(), time.time() + 30 * 60


class Tokens:
    """Access tokens for the service account, by scope, each kept until shortly before it
    expires. `source` signs in as whoever may act as the account."""

    def __init__(self, account: str, source, fetch=call):
        self.account = account
        self.source = source
        self.fetch = fetch
        self._lock = threading.RLock()     # a scope's token asks for the base token inside it
        self._held: dict[str, tuple[str, float]] = {}

    def _fresh(self, key: str, make) -> str:
        with self._lock:
            token, until = self._held.get(key, ("", 0.0))
            if time.time() < until - 120:
                return token
            token, until = make()
            self._held[key] = (token, until)
            return token

    def get(self, scope: str) -> str:
        def impersonate():
            base = self._fresh("", self.source)
            got = self.fetch("POST", f"{IAM_CREDENTIALS}/{urllib.parse.quote(self.account)}:generateAccessToken",
                             base, body={"scope": [scope], "lifetime": "3600s"})
            until = datetime.fromisoformat(got["expireTime"].replace("Z", "+00:00")).timestamp()
            return got["accessToken"], until
        return self._fresh(scope, impersonate)

    def request(self, method: str, url: str, scope: str = READ, body=None) -> dict:
        return self.fetch(method, url, self.get(scope), body=body)


# ---------------------------------------------------------------- which property is a site's


def hosts_of(site) -> list[str]:
    """The site's public host names (no wildcards, no dev server), www. ones included. A
    catch-all (`*.cartercole.net`) has none: its pages are whichever hosts no site is."""
    if site.id.startswith("*."):
        return []
    seen = []
    for name in (site.host, site.id, *site.aliases):
        name = (name or "").lower().rstrip(".")
        if name and "*" not in name and "." in name and name not in seen:
            seen.append(name)
    return seen


def property_for(host: str, properties: list[str]) -> str | None:
    """The most specific property that covers a host: a URL-prefix property for exactly
    it, else the domain property for the longest domain it is under."""
    for scheme in ("https", "http"):
        if f"{scheme}://{host}/" in properties:
            return f"{scheme}://{host}/"
    best = None
    for p in properties:
        if p.startswith("sc-domain:"):
            domain = p[len("sc-domain:"):].lower()
            if (host == domain or host.endswith("." + domain)) and (not best or len(p) > len(best)):
                best = p
    return best


def plan(site, properties: list[str]) -> list[tuple[str, list[str]]]:
    """(property, the site's hosts it covers) for every property the site needs."""
    out: dict[str, list[str]] = {}
    for host in hosts_of(site):
        prop = property_for(host, properties)
        if prop:
            out.setdefault(prop, []).append(host)
    return list(out.items())


def page_filter(prop: str, hosts: list[str]) -> list[dict]:
    """A domain property counts every host under it: keep the site's. A URL-prefix
    property for one host is the host already."""
    if not prop.startswith("sc-domain:"):
        return []
    pattern = "^https?://(" + "|".join(re.escape(h) for h in hosts) + ")(:[0-9]+)?(/|$)"
    return [{"filters": [{"dimension": "page", "operator": "includingRegex", "expression": pattern}]}]


# ---------------------------------------------------------------- numbers


def _merge(groups: list[list[dict]]) -> list[dict]:
    """Rows from several properties as one list: clicks and impressions add up, position
    is the impression-weighted mean, as Google gives it."""
    acc: dict[str, list[float]] = {}
    for rows in groups:
        for r in rows:
            key = r["keys"][0] if r.get("keys") else ""
            a = acc.setdefault(key, [0, 0, 0.0])
            a[0] += r.get("clicks", 0)
            a[1] += r.get("impressions", 0)
            a[2] += r.get("position", 0) * r.get("impressions", 0)
    out = [{"key": k, "clicks": int(c), "impressions": int(i), "ctr": c / i if i else 0.0,
            "position": round(p / i, 2) if i else None} for k, (c, i, p) in acc.items()]
    out.sort(key=lambda r: (-r["clicks"], -r["impressions"], r["key"]))
    return out


def pacific_today(now: datetime | None = None):
    """Search Console's days are Pacific Time ones; eight hours behind UTC is close enough."""
    return ((now or datetime.now(timezone.utc)) - timedelta(hours=8)).date()


class SearchConsole:
    """Search Console for every site, through `request(method, url, scope, body)`
    (Tokens.request, or a stand-in). Reports are kept a quarter of an hour."""

    def __init__(self, request, account: str = ""):
        self.request = request
        self.account = account
        self._lock = threading.Lock()
        self._properties: tuple[float, list[dict]] = (0.0, [])
        self._reports: dict[tuple, tuple[float, dict]] = {}

    @classmethod
    def from_env(cls, account: str, env=os.environ) -> "SearchConsole | None":
        """GOOGLE_AUDIENCE set: sign in as this Lambda's role. Otherwise gcloud, except on
        Lambda, which has none."""
        if not account:
            return None
        audience = env.get("GOOGLE_AUDIENCE", "")
        if audience:
            return cls(Tokens(account, from_aws(audience, env)).request, account)
        if env.get("AWS_LAMBDA_FUNCTION_NAME"):
            return None
        return cls(Tokens(account, from_gcloud).request, account)

    # -- what the account can see

    def properties(self) -> list[dict]:
        """Every property the account can read: [{site_url, permission}], newest list.
        None isn't kept, so access just given in Search Console shows at once."""
        at, props = self._properties
        if time.time() - at < PROPERTIES_TTL and at:
            return props
        got = self.request("GET", f"{API}/sites", READ)
        props = sorted(({"site_url": e["siteUrl"], "permission": e.get("permissionLevel", "")}
                        for e in got.get("siteEntry", []) if e.get("permissionLevel") != "siteUnverifiedUser"),
                       key=lambda p: p["site_url"])
        if props:
            self._properties = (time.time(), props)
        return props

    def _query(self, prop: str, body: dict) -> list[dict]:
        url = f"{API}/sites/{urllib.parse.quote(prop, safe='')}/searchAnalytics/query"
        return self.request("POST", url, READ, body).get("rows", [])

    def _sitemaps(self, prop: str, hosts: list[str]) -> list[dict]:
        got = self.request("GET", f"{API}/sites/{urllib.parse.quote(prop, safe='')}/sitemaps", READ)
        out = []
        for s in got.get("sitemap", []):
            if (urllib.parse.urlsplit(s.get("path", "")).hostname or "").lower() not in hosts:
                continue
            contents = s.get("contents") or []
            out.append({"path": s.get("path", ""), "read": s.get("lastDownloaded", ""),
                        "submitted_at": s.get("lastSubmitted", ""), "pending": bool(s.get("isPending")),
                        "index": bool(s.get("isSitemapsIndex")), "errors": int(s.get("errors") or 0),
                        "warnings": int(s.get("warnings") or 0),
                        "urls": sum(int(c.get("submitted") or 0) for c in contents)})
        return out

    # -- a site's report

    def report(self, site, days: int, now: datetime | None = None) -> dict:
        key = (site.id, days)
        with self._lock:
            at, cached = self._reports.get(key, (0.0, None))
        if cached is not None and time.time() - at < REPORT_TTL:
            return cached
        found = self._report(site, days, now)
        if found["sources"]:  # a site with no property yet may have one on the next look
            with self._lock:
                self._reports[key] = (time.time(), found)
        return found

    def _report(self, site, days: int, now: datetime | None) -> dict:
        props = self.properties()
        mine = plan(site, [p["site_url"] for p in props])
        base = {"connected": True, "account": self.account, "properties": props,
                "hosts": hosts_of(site), "sources": [{"property": p, "hosts": h} for p, h in mine]}
        if not mine:
            return {**base, "totals": None}
        end = pacific_today(now)
        start = end - timedelta(days=days - 1)
        # The days come from a week at least, to say how far behind Google is.
        early = end - timedelta(days=max(days, 7) - 1)

        def body(dimension: str, since, prop: str, hosts: list[str]) -> dict:
            return {"startDate": since.isoformat(), "endDate": end.isoformat(), "dimensions": [dimension],
                    "rowLimit": ROWS, "dataState": "all", "dimensionFilterGroups": page_filter(prop, hosts)}

        jobs = []
        for prop, hosts in mine:
            jobs.append(("date", self._query, prop, body("date", early, prop, hosts)))
            jobs += [(d, self._query, prop, body(d, start, prop, hosts)) for d in DIMENSIONS]
            jobs.append(("sitemaps", self._sitemaps, prop, hosts))
        with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as pool:
            done = [(name, pool.submit(fn, prop, arg)) for name, fn, prop, arg in jobs]
            results: dict[str, list] = {}
            for name, future in done:
                results.setdefault(name, []).append(future.result())

        dates = {r["key"]: r for r in _merge(results["date"])}
        latest = max(dates) if dates else None
        series = []
        for i in range(days):
            day = (start + timedelta(days=i)).isoformat()
            r = dates.get(day) or {"clicks": 0, "impressions": 0, "ctr": 0.0, "position": None}
            series.append({"day": day, **{k: r[k] for k in ("clicks", "impressions", "ctr", "position")}})
        clicks = sum(r["clicks"] for r in series)
        shown = sum(r["impressions"] for r in series)
        weighted = sum((r["position"] or 0) * r["impressions"] for r in series)
        totals = {"clicks": clicks, "impressions": shown, "ctr": clicks / shown if shown else 0.0,
                  "position": round(weighted / shown, 1) if shown else None}
        return {**base, "range": {"start": start.isoformat(), "end": end.isoformat()}, "latest": latest,
                "totals": totals, "series": series,
                "queries": _merge(results["query"]), "pages": _merge(results["page"]),
                "countries": _merge(results["country"]), "devices": _merge(results["device"]),
                "sitemaps": sorted((s for group in results["sitemaps"] for s in group), key=lambda s: s["path"])}
