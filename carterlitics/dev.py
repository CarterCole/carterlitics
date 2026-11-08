"""Run the collector locally, against memory instead of DynamoDB and S3.

    python3 -m carterlitics.dev                    # http://127.0.0.1:8795, carterlitics.toml
    python3 -m carterlitics.dev --port 8795 --config carterlitics.toml --demo

A site's dev server forwards `/_c/*` here with an `x-carterlitics-site` header, the way
CloudFront does in production, so beacons, the console and replays all work on the
site's own origin. Sign-in is a shortcut: /_c/auth/login makes you the administrator.
Set GEO_MMDB to a .mmdb file (or several, comma-separated) for country and network.

    POST /_c/dev/logs   CloudFront log text (W3C or JSON lines), counted as if S3 had
                        delivered it: the way to see the server-side reports locally.
"""
from __future__ import annotations

import argparse
import http.cookies
import http.server
import os
import secrets
import sys
import urllib.parse
from pathlib import Path

from .app import App
from .backends import MemoryBlobs, MemoryTable
from .config import Config
from .geo import Geolocator
from .store import Store
from .web import Request


def make_handler(app: App):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            if "--verbose" in sys.argv:
                super().log_message(fmt, *args)

        def _serve(self):
            url = urllib.parse.urlsplit(self.path)
            jar = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
            length = int(self.headers.get("Content-Length") or 0)
            headers = {k.lower(): v for k, v in self.headers.items()}
            # What CloudFront would add; a forwarding dev server may already have.
            headers.setdefault("cloudfront-viewer-address", f"{self.client_address[0]}:{self.client_address[1]}")
            req = Request(method=self.command, path=url.path, query=dict(urllib.parse.parse_qsl(url.query)),
                          headers=headers, cookies={k: m.value for k, m in jar.items()},
                          body=self.rfile.read(length) if length else b"")
            try:
                res = app.handle(req)
            except Exception:  # noqa: BLE001
                import traceback

                traceback.print_exc()
                self.send_error(500)
                return
            body = res.body.encode() if isinstance(res.body, str) else res.body
            self.send_response(res.status)
            for k, v in res.headers.items():
                self.send_header(k, v)
            for c in res.cookies:
                self.send_header("Set-Cookie", c)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        do_GET = do_POST = do_HEAD = _serve

    return Handler


def main() -> int:
    here = Path.cwd()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT") or 8795))
    ap.add_argument("--config", default=str(here / "carterlitics.toml"))
    ap.add_argument("--demo", action="store_true", help="start with 30 days of made-up traffic")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    cfg = Config.load(args.config, dev=True, session_secret=secrets.token_hex(32),
                      server_secret="dev-server-secret")
    geo = Geolocator.from_env()
    if args.demo:
        from .demo import configure

        cfg = configure(cfg)
    app = App(cfg, Store(cfg, MemoryTable(), MemoryBlobs(), geo))
    if args.demo:
        from .demo import fill

        fill(app.store, cfg)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(app))
    sites = ", ".join(f"{s.id} ({s.dev_url or 'no dev_url'})" for s in cfg.sites.values())
    print(f"carterlitics dev: http://127.0.0.1:{args.port}{cfg.mount}/  sites: {sites}"
          f"{'  geo: on' if geo else ''}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
