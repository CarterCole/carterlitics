"""The collector: one Lambda behind every site's CloudFront at `/_c/*`.

    GET  /_c/c.js                   the client: page views, events, errors, consent, recording
    GET  /_c/s/<file>               static files, content-hashed and immutable
    POST /_c/i                      a batch from c.js: page views, leaves, events, errors
    POST /_c/r                      a chunk of a consented session recording
    POST /_c/server/events          events from a site's own backend (x-carterlitics-key)
    GET  /_c/admin/                 the console (on the console site; others redirect there)
    GET  /_c/admin/replay           one session recording, played back
    GET  /_c/api/*                  the console's data, for a signed-in administrator
    GET  /_c/auth/login|callback    sign-in at the OpenID Connect issuer
    POST /_c/auth/logout

and, not over HTTP, S3 notifications for new CloudFront access log files, which it
reads and counts (`logs.py`).

CloudFront adds two headers to every request it sends here: `x-origin-verify`, a
secret, so the Function URL is useless on its own, and `x-carterlitics-site`, which
says which site the request came through. Viewers can't set either: CloudFront
overwrites a viewer's header of the same name.
"""
from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import mimetypes
import os
import re
import traceback
import urllib.parse
from pathlib import Path

from . import auth, logs
from .backends import DynamoTable, S3Blobs
from .config import Config
from .geo import Geolocator
from .store import LOG_KINDS, Meta, Store
from .web import NO_STORE, Request, Response, from_lambda, no_content, plain, redirect, reply_json, to_lambda

STATIC = Path(__file__).resolve().parent / "static"
MAX_BODY = 64 * 1024            # a batch from c.js: sendBeacon's limit
MAX_CHUNK_BODY = 3 * 1024 * 1024


# ---------------------------------------------------------------- static files


class Assets:
    """Every file under static/, read once. `url(name)` is its content-hashed URL."""

    def __init__(self, mount: str):
        self.mount = mount
        self.files: dict[str, tuple[bytes, str]] = {}  # served name -> (body, type)
        self.urls: dict[str, str] = {}                 # source name -> hashed URL
        for path in sorted(STATIC.rglob("*")):
            if not path.is_file() or path.name.startswith("."):
                continue
            name = path.relative_to(STATIC).as_posix()
            data = path.read_bytes()
            kind = mimetypes.guess_type(name)[0] or "application/octet-stream"
            if kind.startswith("text/") or kind in ("application/javascript", "application/json"):
                kind += "; charset=utf-8"
            digest = hashlib.sha256(data).hexdigest()[:10]
            stem, dot, ext = name.rpartition(".")
            hashed = f"{stem}-{digest}.{ext}" if dot else f"{name}-{digest}"
            self.files[hashed] = (data, kind)
            self.urls[name] = f"{mount}/s/{hashed}"
        # c.js names the recorder by its hashed URL, so it changes when rrweb does.
        client = self.text("c.js").replace("__RRWEB__", self.urls["vendor/rrweb.min.js"])
        self.client = client.encode()

    def text(self, name: str) -> str:
        return (STATIC / name).read_text(encoding="utf-8")

    def page(self, name: str, **values) -> str:
        """An HTML file with {{asset:x}} replaced by URLs and {{key}} by values (escaped)."""
        import html

        body = self.text(name)
        body = re.sub(r"\{\{asset:([^}]+)\}\}", lambda m: self.urls[m.group(1)], body)
        return re.sub(r"\{\{(\w+)\}\}", lambda m: html.escape(str(values.get(m.group(1), ""))), body)

    def get(self, name: str) -> Response | None:
        hit = self.files.get(name)
        if not hit:
            return None
        return Response(200, hit[0], {"Content-Type": hit[1],
                                      "Cache-Control": "public, max-age=31536000, immutable"})


# ---------------------------------------------------------------- app


class App:
    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store
        self.assets = Assets(cfg.mount)

    # -- helpers

    def site_of(self, req: Request):
        """The site a request came through: CloudFront's header, else (locally) the host."""
        name = req.headers.get("x-carterlitics-site")
        if name:
            return self.cfg.sites.get(name)
        return self.cfg.site_for_host(req.headers.get("host")) if self.cfg.dev else None

    def meta(self, req: Request) -> Meta:
        return Meta(ip=req.ip, user_agent=req.headers.get("user-agent"),
                    country=req.headers.get("cloudfront-viewer-country", ""),
                    asn=req.headers.get("cloudfront-viewer-asn", ""),
                    city=req.headers.get("cloudfront-viewer-city", ""), opted_out=req.opted_out)

    def csp(self) -> str:
        sites = " ".join(o for s in self.cfg.sites.values() for o in s.origins)
        form = f" {self.cfg.issuer}" if self.cfg.issuer else ""
        return ("default-src 'self'; script-src 'self'; "
                f"style-src 'self' 'unsafe-inline' {sites} https://fonts.googleapis.com; "
                f"img-src 'self' data: blob: {sites}; font-src 'self' data: {sites} https://fonts.gstatic.com; "
                "media-src 'self' data: blob:; connect-src 'self'; frame-src 'self' blob:; "
                f"object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'{form}")

    def html(self, name: str, **values) -> Response:
        return Response(200, self.assets.page(name, mount=self.cfg.mount, **values), {
            "Content-Type": "text/html; charset=utf-8", **NO_STORE,
            "Content-Security-Policy": self.csp(), "X-Robots-Tag": "noindex, nofollow",
            "Referrer-Policy": "same-origin", "X-Content-Type-Options": "nosniff",
        })

    @staticmethod
    def big_json(data) -> Response:
        """Console data, gzipped: rrweb JSON and long logs shrink about tenfold, which keeps
        them well under the Function URL's 6 MB response cap."""
        body = json.dumps(data, separators=(",", ":"), default=str).encode()
        headers = {"Content-Type": "application/json; charset=utf-8", **NO_STORE}
        if len(body) > 1024:
            return Response(200, gzip.compress(body, compresslevel=6), {**headers, "Content-Encoding": "gzip"})
        return Response(200, body, headers)

    @staticmethod
    def clamp(value, default: int, low: int, high: int) -> int:
        try:
            return max(low, min(high, int(value)))
        except (TypeError, ValueError):
            return default

    # -- routing

    def handle(self, req: Request) -> Response:
        cfg, m = self.cfg, self.cfg.mount
        if req.path == f"{m}/healthz":
            return plain(200, "ok")
        if cfg.origin_secret and not hmac.compare_digest(
                req.headers.get("x-origin-verify", "").encode(), cfg.origin_secret.encode()):
            return plain(403, "forbidden")
        if not req.path.startswith(m + "/"):
            return plain(404, "not found")
        path = req.path[len(m):]
        route = (req.method, path)

        if req.method in ("GET", "HEAD"):
            if path == "/c.js":
                return Response(200, self.assets.client, {
                    "Content-Type": "text/javascript; charset=utf-8",
                    "Cache-Control": "public, max-age=300, s-maxage=300"})
            if path.startswith("/s/"):
                return self.assets.get(path[3:]) or plain(404, "not found")

        if route == ("POST", "/i"):
            site = self.site_of(req)
            if not site:
                return plain(404, "unknown site")
            if len(req.body) > MAX_BODY:
                return plain(413, "too large")
            data = req.json()
            if not isinstance(data, dict):
                return reply_json({"error": "expected a JSON object"}, 400)
            self.store.ingest(site, data, self.meta(req))
            return no_content()

        if route == ("POST", "/r"):
            site = self.site_of(req)
            if not site:
                return plain(404, "unknown site")
            if req.opted_out:
                return no_content()
            if len(req.body) > MAX_CHUNK_BODY:
                return plain(413, "too large")
            try:
                self.store.chunk(site, req.json(), self.meta(req))
            except ValueError as e:
                return reply_json({"error": str(e)}, 400)
            return no_content()

        if route == ("POST", "/server/events"):
            # Through CloudFront, the key travels as x-carterlitics-key: an origin request
            # policy can't forward Authorization. Straight to the Function URL, either works.
            site = self.site_of(req)
            key = req.headers.get("x-carterlitics-key") or \
                req.headers.get("authorization", "").removeprefix("Bearer ").strip()
            if not site or not cfg.server_secret or not hmac.compare_digest(
                    key.encode(), auth.server_key(cfg.server_secret, site.id).encode()):
                return reply_json({"error": "unauthorized"}, 401)
            data = req.json()
            events = data.get("events") if isinstance(data, dict) else None
            return reply_json({"stored": self.store.server_events(site, events or [], self.meta(req))})

        if path in ("/admin", "/admin/", "/admin/replay"):
            if req.method != "GET":
                return plain(405, "GET only")
            console = cfg.console_url()
            here = self.site_of(req)
            # One console for every site: anywhere else, go there.
            if here and cfg.console_site and here.id != cfg.console_site.id and not cfg.dev:
                target = f"{console}/admin/replay" if path == "/admin/replay" else f"{console}/admin/"
                return redirect(f"{target}?{urllib.parse.urlencode({'site': here.id, **req.query})}")
            if path == "/admin":
                return redirect(f"{m}/admin/")
            return self.html("replay.html" if path == "/admin/replay" else "console.html")

        if path.startswith("/api/"):
            return self.api(req, path[4:])

        if route == ("GET", "/auth/login"):
            return auth.login(req, cfg)
        if route == ("GET", "/auth/callback"):
            return auth.callback(req, cfg)
        if route == ("POST", "/auth/logout"):
            return auth.logout(req, cfg)

        if cfg.dev and route == ("POST", "/dev/logs"):
            # Local only: feed CloudFront log text in, as if S3 had delivered it.
            return reply_json(self.store.ingest_log(logs.parse(req.body)))

        return plain(404, "not found")

    def api(self, req: Request, path: str) -> Response:
        cfg = self.cfg
        user = auth.session_user(req, cfg)
        if not user:
            return reply_json({"error": "sign in"}, 401)
        if req.method != "GET":
            return plain(405, "GET only")
        if path == "/me":
            return reply_json({"user": user, "console": cfg.console,
                               "sites": [{"id": s.id, "url": s.url} for s in cfg.sites.values()],
                               "retention": cfg.retention})
        site = cfg.sites.get(req.query.get("site", "")) or cfg.console_site
        if not site:
            return reply_json({"error": "no sites configured"}, 404)
        q = req.query
        if path == "/report":
            return self.big_json(self.store.report(site, self.clamp(q.get("days"), 30, 1, 400)))
        if path == "/log":
            kind = q.get("kind", "")
            if kind not in LOG_KINDS:
                return reply_json({"error": f"kind is one of {', '.join(LOG_KINDS)}"}, 400)
            days = self.clamp(q.get("days"), 7, 1, 90)
            return self.big_json({"items": self.store.log(site, kind, days, self.clamp(q.get("limit"), 200, 1, 1000))})
        if path == "/sessions":
            return self.big_json({"items": self.store.sessions(site, self.clamp(q.get("days"), 7, 1, 30))})
        if path == "/error":
            found = self.store.error(site, q.get("id", ""))
            return self.big_json(found) if found else reply_json({"error": "not found"}, 404)
        if path == "/recording":
            events = self.store.recording(site, q.get("id"))
            if events is None:
                return reply_json({"error": "not found"}, 404)
            return Response(200, gzip.compress(events.encode(), compresslevel=6), {
                "Content-Type": "application/json; charset=utf-8", "Content-Encoding": "gzip", **NO_STORE})
        return reply_json({"error": "not found"}, 404)

    # -- S3 notifications

    def ingest_s3(self, event: dict, read) -> dict:
        totals = {"files": 0, "lines": 0, "stored": 0}
        for bucket, key in logs.s3_objects(event):
            got = self.store.ingest_log(logs.parse(read(bucket, key)), source=f"{bucket}/{key}")
            totals["files"] += 1
            totals["lines"] += got.get("lines", 0)
            totals["stored"] += got.get("stored", 0)
        print(json.dumps({"event": "logs_ingested", **totals}))
        return totals


# ---------------------------------------------------------------- lambda


_app: App | None = None


def build_from_env() -> App:
    cfg = Config.from_env()
    table, bucket = os.environ.get("TABLE"), os.environ.get("BUCKET")
    store = Store(cfg, DynamoTable(table) if table else None, S3Blobs(bucket) if bucket else None,
                  Geolocator.from_env())
    return App(cfg, store)


def _s3_read(bucket: str, key: str) -> bytes:
    import boto3

    return boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()


def lambda_handler(event, context):
    global _app
    if _app is None:
        _app = build_from_env()
    if isinstance(event, dict) and event.get("Records"):
        return _app.ingest_s3(event, _s3_read)
    try:
        res = _app.handle(from_lambda(event))
    except Exception:  # noqa: BLE001 - one bad request must not look like an outage
        traceback.print_exc()
        res = reply_json({"error": "internal error"}, 500)
    return to_lambda(res)

