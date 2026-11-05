"""The collector, end to end over its HTTP surface, against the in-memory backends.

    python3 -m unittest discover tests
"""
from __future__ import annotations

import base64
import gzip
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from carterlitics import auth, classify, logs  # noqa: E402
from carterlitics.app import App, lambda_handler  # noqa: E402
from carterlitics.backends import MemoryBlobs, MemoryTable  # noqa: E402
from carterlitics.config import Config  # noqa: E402
from carterlitics.geo import Geolocator  # noqa: E402
from carterlitics.store import Store, group_id, redact_query, top_frame, truncate_ip  # noqa: E402
from carterlitics.web import Request, from_lambda  # noqa: E402

SAFARI = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
          "Version/18.1 Safari/605.1.15")
SID = "11111111-2222-4333-8444-555555555555"
VID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
FP = "0123456789abcdef0123456789abcdef"
SECRET = "s" * 32

DATA = {
    "console": "a.example",
    "auth": {"issuer": "https://issuer.example", "admin_role": "god"},
    "site": [
        {"id": "a.example", "url": "https://a.example", "aliases": ["www.a.example"], "exclude": ["/admin"]},
        {"id": "b.example", "url": "https://b.example", "server_ip": "truncate"},
    ],
}


def make(**overrides) -> App:
    cfg = Config.from_data(DATA, session_secret=SECRET, origin_secret="origin", server_secret="server",
                           client_id="client", client_secret="shh", **overrides)
    return App(cfg, Store(cfg, MemoryTable(), MemoryBlobs()))


def body(res):
    raw = res.body if isinstance(res.body, bytes) else res.body.encode()
    if res.headers.get("Content-Encoding") == "gzip":
        raw = gzip.decompress(raw)
    return json.loads(raw) if raw else None


class Client:
    def __init__(self, app: App):
        self.app = app
        self.cookies: dict[str, str] = {}

    def __call__(self, method, path, data=None, site="a.example", ua=SAFARI, ip="8.8.8.8", headers=None,
                 query=None, raw=None):
        h = {"x-origin-verify": "origin", "user-agent": ua, "cloudfront-viewer-address": f"{ip}:5000",
             "cloudfront-viewer-country": "US", **(headers or {})}
        if site:
            h["x-carterlitics-site"] = site
        payload = raw if raw is not None else (json.dumps(data).encode() if data is not None else b"")
        return self.app.handle(Request(method, path, query or {}, h, dict(self.cookies), payload))

    def sign_in(self, sub="did:kith:owner", roles=("god",)):
        self.cookies[auth.SESSION_COOKIE] = auth.sign(SECRET, "session", {
            "sub": sub, "roles": list(roles), "name": "Owner", "exp": int(time.time()) + 600})


class Classify(unittest.TestCase):
    def test_bots(self):
        cases = {
            "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)": ("Googlebot", "search"),
            "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; GPTBot/1.2)": ("GPTBot", "ai"),
            "Mozilla/5.0 (compatible; ClaudeBot/1.0; +claudebot@anthropic.com)": ("ClaudeBot", "ai"),
            "curl/8.7.1": ("curl", "library"),
            "python-requests/2.32": ("python-requests", "library"),
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 HeadlessChrome/120": ("HeadlessChrome", "automation"),
            "facebookexternalhit/1.1": ("Facebook", "social"),
            "Feedly/1.0 (+http://www.feedly.com/fetcher.html)": ("Feedly", "feed"),
            "SomeTool/3.1": ("SomeTool", "other"),
            "": ("(no user agent)", "other"),
        }
        for ua, (name, category) in cases.items():
            bot = classify.bot_of(ua)
            self.assertIsNotNone(bot, ua)
            self.assertEqual((bot.name, bot.category), (name, category), ua)
        self.assertIsNone(classify.bot_of(SAFARI, "/"))
        # A browser user agent asking for WordPress files is a scanner anyway.
        self.assertEqual(classify.bot_of(SAFARI, "/wp-login.php").name, "probe")
        self.assertEqual(classify.bot_of(SAFARI, "/.env").category, "scanner")

    def test_browsers(self):
        self.assertEqual(classify.browser_of(SAFARI), ("Safari", "macOS"))
        self.assertEqual(classify.browser_of(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36 Edg/131"),
            ("Edge", "Windows"))
        self.assertEqual(classify.browser_of(
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 CriOS/131 Mobile/15E148"),
            ("Chrome", "iOS"))

    def test_paths(self):
        self.assertEqual(classify.clean_path("/mosaic/"), "/mosaic")
        self.assertEqual(classify.clean_path("/mosaic.html"), "/mosaic")
        self.assertEqual(classify.clean_path("/blog/index.html"), "/blog")
        self.assertEqual(classify.clean_path("/index.html"), "/")
        self.assertEqual(classify.clean_path("/x?y=1"), "/x")
        self.assertIsNone(classify.clean_path("/admin/x", ("/admin",)))
        self.assertEqual(classify.clean_path("/administrator", ("/admin",)), "/administrator")
        self.assertIsNone(classify.clean_path("/_c/admin/"))
        self.assertIsNone(classify.clean_path("/a b"))
        self.assertIsNone(classify.clean_path("relative"))
        self.assertTrue(classify.is_asset("/app-1234.js"))
        self.assertFalse(classify.is_asset("/rss.xml"))

    def test_referrers(self):
        own = frozenset({"a.example"})
        self.assertEqual(classify.referrer_of("https://www.News.example.com:443/x?y", own), "news.example.com")
        self.assertEqual(classify.referrer_of("https://www.a.example/blog", own), "direct")
        self.assertEqual(classify.referrer_of(None, own), "direct")

    def test_errors_group_across_builds(self):
        a = "TypeError: x is undefined\n    at go (https://a.example/app-3f9a1c2e.js:10:5)"
        b = "TypeError: x is undefined\n    at go (https://a.example/app-77aa88bb.js:12:9)"
        self.assertEqual(top_frame(a), "go@app.js")
        self.assertEqual(group_id("error", "TypeError", "x is undefined at 42", a, ""),
                         group_id("error", "TypeError", "x is undefined at 97", b, ""))
        self.assertNotEqual(group_id("error", "TypeError", "y is undefined", a, ""),
                            group_id("error", "TypeError", "x is undefined", a, ""))

    def test_redact_query(self):
        self.assertEqual(redact_query("code=abc&state=xyz&next=/blog"), "code=…&state=…&next=/blog")
        self.assertEqual(redact_query("X-Amz-Signature=f00&page=2"), "X-Amz-Signature=…&page=2")
        self.assertEqual(redact_query(""), "")

    def test_wildcard_hosts(self):
        cfg = Config.from_data({"site": [{"id": "*.a.example", "url": "https://a.example", "aliases": ["*.a.example"]},
                                         {"id": "a.example", "url": "https://a.example"}]})
        self.assertEqual(cfg.site_for_host("www.a.example").id, "a.example")
        self.assertEqual(cfg.site_for_host("probe.a.example").id, "*.a.example")
        self.assertIsNone(cfg.site_for_host("a.example.evil"))

    def test_truncate(self):
        self.assertEqual(truncate_ip("198.51.100.7"), "198.51.100.0")
        self.assertEqual(truncate_ip("2001:db8:1:2::5"), "2001:db8:1::")


class Ingest(unittest.TestCase):
    def setUp(self):
        self.app = make()
        self.call = Client(self.app)

    def report(self, site="a.example", days=1):
        self.call.sign_in()
        return body(self.call("GET", "/_c/api/report", query={"site": site, "days": str(days)}))

    def test_refuses_without_the_origin_secret(self):
        res = self.call("POST", "/_c/i", {"items": []}, headers={"x-origin-verify": "nope"})
        self.assertEqual(res.status, 403)
        self.assertEqual(self.call("GET", "/_c/healthz", headers={"x-origin-verify": ""}).status, 200)

    def test_unknown_site(self):
        self.assertEqual(self.call("POST", "/_c/i", {"items": []}, site="evil.example").status, 404)

    def test_anonymous_page_views(self):
        self.call("POST", "/_c/i", {"w": 1400, "items": [
            {"t": "pv", "p": "/x/", "r": "https://news.example/item"},
            {"t": "pv", "p": "/admin"},             # excluded
            {"t": "leave", "p": "/x", "ms": 4000},
            {"t": "ev", "n": "signup", "props": {"plan": "pro"}},
        ]})
        self.call("POST", "/_c/i", {"w": 390, "items": [{"t": "pv", "p": "/"}]}, ip="1.1.1.1")
        r = self.report()
        self.assertEqual(r["totals"]["pv"], 2)
        self.assertEqual(r["totals"]["uv"], 2)
        self.assertEqual(r["totals"]["ev"], 1)
        paths = {p["name"]: p for p in r["top"]["paths"]}
        self.assertEqual(paths["/x"]["avg_ms"], 4000)
        self.assertEqual({d["name"] for d in r["top"]["devices"]}, {"desktop", "mobile"})
        self.assertIn({"name": "news.example", "value": 1}, r["top"]["referrers"])
        # Nothing identifying was stored for anonymous visitors.
        self.assertEqual(body(self.call("GET", "/_c/api/log", query={"site": "a.example", "kind": "pv"}))["items"], [])
        events = body(self.call("GET", "/_c/api/log", query={"site": "a.example", "kind": "ev"}))["items"]
        self.assertEqual(events[0]["props"], {"plan": "pro"})
        self.assertNotIn("vid", events[0])
        self.assertNotIn("ip", events[0])

    def test_the_same_visitor_counts_once_a_day(self):
        for _ in range(3):
            self.call("POST", "/_c/i", {"items": [{"t": "pv", "p": "/"}]})
        self.assertEqual(self.report()["totals"], {**self.report()["totals"], "pv": 3, "uv": 1})

    def test_do_not_track(self):
        self.call("POST", "/_c/i", {"items": [{"t": "pv", "p": "/"}, {"t": "ev", "n": "x"},
                                              {"t": "err", "m": "boom"}]}, headers={"sec-gpc": "1"})
        r = self.report()
        self.assertEqual((r["totals"]["pv"], r["totals"]["ev"], r["totals"]["err"]), (0, 0, 1))

    def test_bots_are_not_people(self):
        self.call("POST", "/_c/i", {"items": [{"t": "pv", "p": "/"}]}, ua="Mozilla/5.0 (compatible; Googlebot/2.1)")
        self.call("POST", "/_c/i", {"wd": True, "items": [{"t": "pv", "p": "/"}]})
        r = self.report()
        self.assertEqual((r["totals"]["pv"], r["totals"]["jsbot"]), (0, 2))

    def test_consented_visitor_session_and_recording(self):
        ids = {"vid": VID, "sid": SID, "fp": FP, "sd": time.strftime("%Y-%m-%d", time.gmtime())}
        self.call("POST", "/_c/i", {**ids, "w": 1200, "items": [{"t": "pv", "p": "/", "new": True}]})
        self.call("POST", "/_c/i", {**ids, "uid": "user-1", "items": [
            {"t": "pv", "p": "/x"}, {"t": "leave", "p": "/x", "ms": 2000}, {"t": "err", "m": "boom"}]})
        # Consent given mid-page: logged for the session, not counted again.
        self.call("POST", "/_c/i", {**ids, "items": [{"t": "pv", "p": "/x", "dup": True}]})
        self.assertEqual(self.call("POST", "/_c/r", {"sid": SID, "vid": VID, "seq": 0, "events": [
            {"type": 4, "timestamp": 1}, {"type": 2, "timestamp": 2}]}).status, 204)
        self.assertEqual(self.call("POST", "/_c/r", {"sid": SID, "vid": VID, "seq": 1, "events": [
            {"type": 3, "timestamp": 3}]}).status, 204)
        self.assertEqual(self.report()["totals"]["pv"], 2)
        sessions = body(self.call("GET", "/_c/api/sessions", query={"site": "a.example"}))["items"]
        self.assertEqual(len(sessions), 1)
        s = sessions[0]
        self.assertEqual((s["id"], s["pages"], s["errors"], s["ms"], s["rec"], s["uid"], s["ip"]),
                         (SID, 3, 1, 2000, 1, "user-1", "8.8.8.8"))
        views = body(self.call("GET", "/_c/api/log", query={"site": "a.example", "kind": "pv"}))["items"]
        self.assertEqual({v["fp"] for v in views}, {FP})
        res = self.call("GET", "/_c/api/recording", query={"site": "a.example", "id": SID})
        self.assertEqual([e["timestamp"] for e in body(res)], [1, 2, 3])
        # Another site can't read it.
        self.assertEqual(self.call("GET", "/_c/api/recording", query={"site": "b.example", "id": SID}).status, 404)

    def test_bad_identifiers_are_anonymous(self):
        self.call("POST", "/_c/i", {"vid": "x#y", "sid": SID, "items": [{"t": "pv", "p": "/"}]})
        self.call("POST", "/_c/i", {"vid": VID, "sid": SID, "fp": "nope", "items": [{"t": "pv", "p": "/"}]})
        self.assertEqual(self.report()["totals"]["pv"], 2)
        self.assertEqual(body(self.call("GET", "/_c/api/sessions", query={"site": "a.example"}))["items"], [])
        self.assertEqual(self.call("POST", "/_c/r", {"sid": "x", "vid": VID, "seq": 0, "events": [{}]}).status, 400)

    def test_limits(self):
        self.assertEqual(self.call("POST", "/_c/i", raw=b"x" * 70000).status, 413)
        self.assertEqual(self.call("POST", "/_c/i", raw=b"not json").status, 400)
        self.call("POST", "/_c/i", {"items": [{"t": "pv", "p": "/"}] * 80})
        self.assertEqual(self.report()["totals"]["pv"], 50)

    def test_errors_group_and_detail(self):
        stack = "TypeError: a\n    at f (https://a.example/app-1234abcd.js:1:2)"
        for n in (1, 2):
            self.call("POST", "/_c/i", {"items": [{"t": "err", "k": "error", "name": "TypeError",
                                                   "m": f"a is undefined ({n})", "st": stack, "p": "/x"}]})
        r = self.report()
        self.assertEqual(len(r["groups"]), 1)
        g = r["groups"][0]
        self.assertEqual((g["count"], g["name"]), (2, "TypeError"))
        detail = body(self.call("GET", "/_c/api/error", query={"site": "a.example", "id": g["id"]}))
        self.assertEqual(len(detail["occurrences"]), 2)
        self.assertEqual(detail["group"]["stack"], stack)
        self.assertIn("ua", detail["occurrences"][0])
        self.assertNotIn("vid", detail["occurrences"][0])

    def test_server_events_need_the_key(self):
        events = {"events": [{"name": "signup", "props": {"via": "google"}, "ip": "8.8.4.4", "uid": "u1"}]}
        self.assertEqual(self.call("POST", "/_c/server/events", events).status, 401)
        key = auth.server_key("server", "a.example")
        res = self.call("POST", "/_c/server/events", events, headers={"x-carterlitics-key": key})
        self.assertEqual(body(res), {"stored": 1})
        other = auth.server_key("server", "b.example")
        self.assertEqual(self.call("POST", "/_c/server/events", events,
                                   headers={"authorization": f"Bearer {other}"}).status, 401)
        self.assertEqual(self.report()["top"]["events"], [{"name": "signup", "value": 1}])


LOG_W3C = """#Version: 1.0
#Fields: date time x-edge-location sc-bytes c-ip cs-method cs(Host) cs-uri-stem sc-status cs(Referer) cs(User-Agent) cs-uri-query cs(Cookie) x-edge-result-type x-edge-request-id x-host-header cs-protocol cs-bytes time-taken
{day}\t03:12:45\tIAD89-C1\t1234\t203.0.113.9\tGET\td1.cloudfront.net\t/wp-login.php\t404\t-\tMozilla/5.0%20(Windows%20NT%2010.0)\t-\t-\tError\tabc\ta.example\thttps\t100\t0.002
{day}\t03:12:46\tIAD89-C1\t1234\t66.249.66.1\tGET\td1.cloudfront.net\t/blog\t200\t-\tMozilla/5.0%20(compatible;%20Googlebot/2.1;%20+http://www.google.com/bot.html)\t-\t-\tHit\tabc\ta.example\thttps\t100\t0.001
{day}\t03:12:47\tIAD89-C1\t1234\t198.51.100.7\tGET\td1.cloudfront.net\t/blog/\t200\thttps://news.example/\tMozilla/5.0%20(Macintosh)%20Safari/605\t-\t-\tHit\tabc\twww.a.example\thttps\t100\t0.001
{day}\t03:12:48\tIAD89-C1\t1234\t198.51.100.7\tGET\td1.cloudfront.net\t/app-abc.js\t200\t-\tMozilla/5.0\t-\t-\tHit\tabc\ta.example\thttps\t100\t0.001
{day}\t03:12:49\tIAD89-C1\t1234\t198.51.100.8\tGET\td1.cloudfront.net\t/\t200\t-\tMozilla/5.0%20Safari\t-\t-\tHit\tabc\tb.example\thttps\t100\t0.001
{day}\t03:12:50\tIAD89-C1\t1234\t1.2.3.4\tGET\td1.cloudfront.net\t/\t200\t-\tMozilla/5.0\t-\t-\tHit\tabc\tother.example\thttps\t100\t0.001
"""


class ServerLogs(unittest.TestCase):
    def setUp(self):
        self.app = make()
        self.call = Client(self.app)
        self.call.sign_in()
        self.day = time.strftime("%Y-%m-%d", time.gmtime())

    def test_w3c(self):
        lines = list(logs.parse(gzip.compress(LOG_W3C.format(day=self.day).encode())))
        self.assertEqual(len(lines), 6)
        self.assertEqual(lines[1].user_agent, "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)")
        got = self.app.store.ingest_log(lines, source="bucket/key")
        self.assertEqual(got, {"lines": 6, "stored": 4, "skipped": 2})
        # S3 can deliver a notification twice; the file is only counted once.
        self.assertTrue(self.app.store.ingest_log(lines, source="bucket/key")["duplicate"])
        r = body(self.call("GET", "/_c/api/report", query={"site": "a.example", "days": "1"}))
        self.assertEqual((r["totals"]["req"], r["totals"]["hum"], r["totals"]["bot"]), (3, 1, 2))
        self.assertEqual({b["name"] for b in r["top"]["bots"]}, {"Googlebot", "probe"})
        self.assertEqual(r["top"]["server_paths"], [{"name": "/blog", "value": 1}])
        bots = body(self.call("GET", "/_c/api/log", query={"site": "a.example", "kind": "bot"}))["items"]
        self.assertEqual({b["path"] for b in bots}, {"/wp-login.php", "/blog"})
        people = body(self.call("GET", "/_c/api/log", query={"site": "b.example", "kind": "hum"}))["items"]
        self.assertEqual(people[0]["ip"], "198.51.100.0")  # b.example keeps truncated addresses

    def test_json_lines(self):
        line = json.dumps({"timestamp": str(time.time()), "c-ip": "203.0.113.5", "cs-method": "GET",
                           "x-host-header": "a.example", "cs-uri-stem": "/feed.xml", "sc-status": "200",
                           "cs(User-Agent)": "Feedly/1.0", "c-country": "DE", "asn": "24940"})
        lines = list(logs.parse(line.encode()))
        self.assertEqual((lines[0].path, lines[0].country, lines[0].asn), ("/feed.xml", "DE", "24940"))
        self.app.store.ingest_log(lines)
        r = body(self.call("GET", "/_c/api/report", query={"site": "a.example", "days": "1"}))
        self.assertEqual(r["top"]["bots"], [{"name": "Feedly", "value": 1}])

    def test_lambda_s3_event(self):
        data = LOG_W3C.format(day=self.day).encode()
        event = {"Records": [{"eventSource": "aws:s3", "eventName": "ObjectCreated:Put",
                              "s3": {"bucket": {"name": "logs"}, "object": {"key": "logs/a.example/x.gz"}}}]}
        got = self.app.ingest_s3(event, lambda bucket, key: data)
        self.assertEqual(got, {"files": 1, "lines": 6, "stored": 4})


class Console(unittest.TestCase):
    def setUp(self):
        self.app = make()
        self.call = Client(self.app)

    def test_api_needs_an_admin(self):
        self.assertEqual(self.call("GET", "/_c/api/report").status, 401)
        self.call.sign_in(roles=("member",))
        self.assertEqual(self.call("GET", "/_c/api/report").status, 401)
        self.call.sign_in()
        self.assertEqual(self.call("GET", "/_c/api/me").status, 200)

    def test_console_redirects_to_the_console_site(self):
        res = self.call("GET", "/_c/admin/", site="b.example")
        self.assertEqual(res.status, 302)
        self.assertEqual(res.headers["Location"], "https://a.example/_c/admin/?site=b.example")
        page = self.call("GET", "/_c/admin/")
        self.assertEqual(page.status, 200)
        self.assertIn("default-src 'self'", page.headers["Content-Security-Policy"])
        self.assertIn("https://b.example", page.headers["Content-Security-Policy"])
        self.assertIn("/_c/s/console-", page.body)

    def test_static(self):
        js = self.call("GET", "/_c/c.js")
        self.assertEqual(js.status, 200)
        self.assertNotIn(b"__RRWEB__", js.body)
        self.assertIn(b"/_c/s/vendor/rrweb.min-", js.body)
        url = self.app.assets.urls["console.js"]
        res = self.call("GET", url)
        self.assertEqual(res.status, 200)
        self.assertIn("immutable", res.headers["Cache-Control"])
        self.assertEqual(self.call("GET", "/_c/s/nope.js").status, 404)

    def test_sign_in(self):
        res = self.call("GET", "/_c/auth/login", query={"next": "/_c/admin/#errors"})
        self.assertEqual(res.status, 302)
        self.assertTrue(res.headers["Location"].startswith("https://issuer.example/oauth/authorize?"))
        flow = res.cookies[0].split(";")[0].split("=", 1)[1]
        state = dict(p.split("=") for p in res.headers["Location"].split("?")[1].split("&"))["state"]
        nonce = auth.unsign(SECRET, "flow", flow)["nonce"]
        claims = {"iss": "https://issuer.example", "aud": "client", "sub": "did:kith:owner", "nonce": nonce,
                  "exp": time.time() + 60, "roles": ["god"], "name": "Owner"}
        token = "x." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".y"

        def fetch(url, **kw):
            if url.endswith("openid-configuration"):
                raise RuntimeError("offline")
            return {"id_token": token}

        self.call.cookies[auth.FLOW_COOKIE] = flow
        with mock.patch.dict(auth._discovery, clear=True):
            res = auth.callback(Request("GET", "/_c/auth/callback", {"state": state, "code": "c"}, {},
                                        {auth.FLOW_COOKIE: flow}), self.app.cfg, fetch=fetch)
        self.assertEqual(res.status, 302)
        self.assertEqual(res.headers["Location"], "/_c/admin/#errors")
        session = [c for c in res.cookies if c.startswith(auth.SESSION_COOKIE + "=")][0]
        self.assertIn("Path=/_c/", session)
        self.assertIn("HttpOnly", session)
        # Not an admin: no session.
        claims["roles"] = []
        token = "x." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".y"
        with mock.patch.dict(auth._discovery, clear=True):
            res = auth.callback(Request("GET", "/_c/auth/callback", {"state": state, "code": "c"}, {},
                                        {auth.FLOW_COOKIE: flow}), self.app.cfg, fetch=fetch)
        self.assertEqual(res.status, 403)

    def test_safe_next(self):
        cfg = self.app.cfg
        self.assertEqual(auth.safe_next(cfg, "https://evil.example/_c/"), "/_c/admin/")
        self.assertEqual(auth.safe_next(cfg, "//evil.example/_c/x"), "/_c/admin/")
        self.assertEqual(auth.safe_next(cfg, "/_c/admin/replay?id=1"), "/_c/admin/replay?id=1")


class LambdaAdapter(unittest.TestCase):
    def test_function_url_event(self):
        event = {"rawPath": "/_c/i", "rawQueryString": "", "requestContext": {"http": {"method": "POST"}},
                 "headers": {"X-Origin-Verify": "origin", "x-carterlitics-site": "a.example"},
                 "cookies": ["a=1", "b=2"], "body": base64.b64encode(b'{"items":[]}').decode(),
                 "isBase64Encoded": True}
        req = from_lambda(event)
        self.assertEqual((req.method, req.cookies, req.json()), ("POST", {"a": "1", "b": "2"}, {"items": []}))
        env = {"CARTERLITICS_CONFIG": json.dumps(DATA), "ORIGIN_SECRET": "origin"}
        with mock.patch.dict(os.environ, env, clear=False), mock.patch("carterlitics.app._app", None):
            out = lambda_handler(event, None)
        self.assertEqual(out["statusCode"], 204)


@unittest.skipUnless(os.environ.get("GEO_MMDB"), "set GEO_MMDB to test against a real database")
class Geo(unittest.TestCase):
    def test_lookup(self):
        geo = Geolocator.from_env()
        self.assertEqual(geo.lookup("8.8.8.8").asn, "15169")
        self.assertEqual(geo.lookup("192.168.1.1").asn, "")
        self.assertEqual(geo.lookup("not an ip").country, "")


if __name__ == "__main__":
    unittest.main()
