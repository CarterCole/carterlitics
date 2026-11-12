"""The collector, end to end over its HTTP surface, against the in-memory backends.

    python3 -m unittest discover tests
"""
from __future__ import annotations

import base64
import gzip
import json
import os
import shutil
import struct
import subprocess
import sys
import time
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from carterlitics import auth, classify, intel, logs  # noqa: E402
from carterlitics import store as store_module  # noqa: E402
from carterlitics.app import App, lambda_handler  # noqa: E402
from carterlitics.backends import MemoryBlobs, MemoryTable  # noqa: E402
from carterlitics.config import Config, Step  # noqa: E402
from carterlitics.geo import Geolocator  # noqa: E402
from carterlitics.intel import Intel, Intelligence, Ranges, build_ranges, verify  # noqa: E402
from carterlitics.store import LogLine, Store, group_id, redact_query, top_frame, truncate_ip  # noqa: E402
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


# What the published lists and reverse DNS say about the tests' addresses.
RANGES = [("crawler:Googlebot", "66.249.64.0/19"), ("relay:iCloud Private Relay", "172.224.0.0/12"),
          ("cloud:AWS", "52.0.0.0/11"), ("cloud:AWS EC2 us-east-1", "52.4.0.0/14"),
          ("drop:Spamhaus DROP", "1.10.16.0/20"), ("abuse:blocklist.de", "203.0.113.0/24"),
          ("tor:Tor exit", "2001:db8:7::/48")]
NAMES = {"66.249.66.1": "crawl-66-249-66-1.googlebot.com", "52.4.1.1": "ec2-52-4-1-1.compute-1.amazonaws.com",
         "198.51.100.7": "c-198-51-100-7.hsd1.il.comcast.net", "198.51.100.8": "host8.b.example",
         "8.8.8.8": "dns.google", "52.4.9.9": "crawl-52-4-9-9.googlebot.com"}


def fake_intel(table=None, calls=None) -> Intelligence:
    calls = calls if calls is not None else []

    def reverse(ip, timeout):
        calls.append(ip)
        return NAMES.get(ip, "")

    # 52.4.9.9 (on AWS) named itself like Googlebot: the name doesn't resolve back.
    return Intelligence(Ranges(build_ranges(RANGES, "test")), table, dns=True, reverse=reverse,
                        forward=lambda host, ip, timeout: NAMES.get(ip) == host and ip != "52.4.9.9")


def make(with_intel: bool = False, **overrides) -> App:
    cfg = Config.from_data(DATA, session_secret=SECRET, origin_secret="origin", server_secret="server",
                           client_id="client", client_secret="shh", **overrides)
    table = MemoryTable()
    return App(cfg, Store(cfg, table, MemoryBlobs(), intel=fake_intel(table) if with_intel else None))


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

    def test_sources(self):
        own = frozenset({"a.example"})

        def of(referrer, **tags):
            s = classify.source_of(referrer, tags, own)
            return s.name, s.channel, s.campaign

        self.assertEqual(of(None), ("Direct", "Direct", ""))
        self.assertEqual(of("https://www.a.example/blog"), ("Direct", "Direct", ""))
        # Grouped by who it is, the most specific host first.
        self.assertEqual(of("https://www.google.co.uk/"), ("Google", "Search", ""))
        self.assertEqual(of("android-app://com.google.android.googlequicksearchbox/"), ("Google", "Search", ""))
        self.assertEqual(of("https://mail.google.com/mail/u/0/"), ("Gmail", "Email", ""))
        self.assertEqual(of("https://gemini.google.com/app"), ("Gemini", "AI assistants", ""))
        self.assertEqual(of("https://l.facebook.com/l.php"), ("Facebook", "Social", ""))
        self.assertEqual(of("https://t.co/abc"), ("X", "Social", ""))
        self.assertEqual(of("https://google.evil.example/"), ("google.evil.example", "Referral", ""))
        self.assertEqual(of("https://blog.example.com/post"), ("blog.example.com", "Referral", ""))
        # A link's own tags win, and name a known source the same way its host does.
        self.assertEqual(of("https://www.google.com/", utm_source="Newsletter", utm_medium="email",
                            utm_campaign="  November   Notes "), ("newsletter", "Email", "november notes"))
        self.assertEqual(of(None, utm_source="chatgpt.com"), ("ChatGPT", "AI assistants", ""))
        self.assertEqual(of("https://news.ycombinator.com/item?id=1", ref="hn"), ("Hacker News", "Social", ""))
        self.assertEqual(of("https://producthunt.example/p", ref="producthunt"), ("producthunt", "Referral", ""))
        self.assertEqual(of(None, ref="producthunt"), ("producthunt", "Other", ""))
        self.assertEqual(of(None, utm_medium="email"), ("(not set)", "Email", ""))
        self.assertEqual(of(None, utm_source="mastodon", utm_medium="social"), ("Mastodon", "Social", ""))
        # Paid: by medium, or an ad click id. fbclid is on every link Facebook opens.
        self.assertEqual(of("https://www.google.com/", utm_source="google", utm_medium="cpc", utm_campaign="launch"),
                         ("Google", "Paid search", "launch"))
        self.assertEqual(of(None, click="gclid"), ("Google", "Paid search", ""))
        self.assertEqual(of(None, click="twclid"), ("X", "Paid social", ""))
        self.assertEqual(of(None, click="fbclid"), ("Facebook", "Social", ""))
        self.assertEqual(of(None, click="nope"), ("Direct", "Direct", ""))
        self.assertEqual(of(None, utm_source="x" * 300)[0], "x" * 100)
        self.assertEqual(classify.source_of("https://bing.com/", "not tags", own).name, "Bing")

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

    def test_consent_version_is_kept(self):
        """The version of the site's policy a visitor accepted goes on everything they send."""
        ids = {"vid": VID, "sid": SID, "sd": time.strftime("%Y-%m-%d", time.gmtime())}
        self.call("POST", "/_c/i", {**ids, "cv": 2, "items": [{"t": "pv", "p": "/", "new": True}, {"t": "ev", "n": "x"}]})
        for bad in (True, "2", -1, 10 ** 7):
            self.call("POST", "/_c/i", {**ids, "cv": bad, "items": [{"t": "ev", "n": "y"}]})
        self.call.sign_in()
        session = body(self.call("GET", "/_c/api/sessions", query={"site": "a.example"}))["items"][0]
        self.assertEqual(session["cv"], 2)
        view, = body(self.call("GET", "/_c/api/log", query={"site": "a.example", "kind": "pv"}))["items"]
        self.assertEqual(view["cv"], 2)
        events = body(self.call("GET", "/_c/api/log", query={"site": "a.example", "kind": "ev"}))["items"]
        self.assertEqual(sorted((e["name"], e.get("cv")) for e in events), [("x", 2)] + [("y", None)] * 4)
        me = body(self.call("GET", "/_c/api/me"))
        self.assertEqual({s["id"]: s["consent_docs"] for s in me["sites"]}, {"a.example": "", "b.example": ""})

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
        self.assertEqual({s["name"]: (s["value"], s["bounce_rate"]) for s in r["top"]["sources"]},
                         {"news.example": (1, 0.0), "Direct": (1, 1.0)})
        self.assertEqual({s["name"] for s in r["top"]["channels"]}, {"Referral", "Direct"})
        self.assertEqual(r["sources_from"], r["series"][-1]["day"])
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
        self.assertEqual((s["id"], s["pages"], s["errors"], s["ms"], s["rec"], s["uid"], s["ip"], s["exit"]),
                         (SID, 3, 1, 2000, 1, "user-1", "8.8.8.8", "/x"))
        views = body(self.call("GET", "/_c/api/log", query={"site": "a.example", "kind": "pv"}))["items"]
        self.assertEqual({v["fp"] for v in views}, {FP})
        res = self.call("GET", "/_c/api/recording", query={"site": "a.example", "id": SID})
        self.assertEqual([e["timestamp"] for e in body(res)], [1, 2, 3])
        # Another site can't read it.
        self.assertEqual(self.call("GET", "/_c/api/recording", query={"site": "b.example", "id": SID}).status, 404)

    def test_a_session_can_begin_with_an_event(self):
        # c.js's session went idle and the visitor came back with a click, not a page view.
        ids = {"vid": VID, "sid": SID, "sd": time.strftime("%Y-%m-%d", time.gmtime())}
        self.call("POST", "/_c/i", {**ids, "items": [{"t": "ev", "n": "outbound", "p": "/x", "new": True}]})
        self.call.sign_in()
        s = body(self.call("GET", "/_c/api/sessions", query={"site": "a.example"}))["items"][0]
        self.assertEqual((s["id"], s["pages"], s["events"], s["path"], s["exit"], s["ref"]),
                         (SID, 0, 1, "/x", "/x", "direct"))

    def visit(self, minute, items, ip="8.8.8.8", **kw):
        # From 01:00 UTC today: one day's salt, so one visitor hash per address.
        at = store_module.now().replace(hour=1, minute=0, second=0, microsecond=0) + timedelta(minutes=minute)
        with mock.patch.object(store_module, "now", lambda: at):
            self.call("POST", "/_c/i", {"items": items}, ip=ip, **kw)

    def test_visits_bounces_and_exits(self):
        pv = lambda p: {"t": "pv", "p": p}  # noqa: E731
        self.visit(0, [pv("/")])                                    # / then /x: one visit
        self.visit(5, [pv("/x")])
        self.visit(0, [pv("/")], ip="1.1.1.1")                      # a bounce, and it stays one:
        self.visit(1, [{"t": "ev", "n": "seen", "props": {"non_interaction": True}}], ip="1.1.1.1")
        self.visit(1, [{"t": "leave", "p": "/", "ms": 60000}], ip="1.1.1.1")
        self.visit(0, [pv("/x"), {"t": "ev", "n": "outbound"}], ip="2.2.2.2")   # a click: not a bounce
        self.visit(36, [pv("/y")])                                  # 31 minutes after /x: a new visit
        self.visit(2, [pv("/")], ip="3.3.3.3", headers={"sec-gpc": "1"})        # opted out: none
        self.visit(2, [pv("/")], ip="4.4.4.4", ua="Mozilla/5.0 (compatible; Googlebot/2.1)")
        r = self.report()
        t = r["totals"]
        self.assertEqual((t["ses"], t["bnc"], t["pv"], t["vpv"]), (4, 2, 5, 5))
        self.assertEqual({e["name"]: (e["value"], e["bounce_rate"]) for e in r["top"]["entries"]},
                         {"/": (2, 0.5), "/x": (1, 0.0), "/y": (1, 1.0)})
        self.assertEqual({e["name"]: (e["value"], e["exit_rate"]) for e in r["top"]["exits"]},
                         {"/x": (2, 1.0), "/": (1, 0.5), "/y": (1, 1.0)})
        self.assertEqual({p["name"]: p["exit_rate"] for p in r["top"]["paths"]}, {"/": 0.5, "/x": 1.0, "/y": 1.0})

    def test_visits_by_source_channel_and_campaign(self):
        pv = lambda p, r=None, **u: {"t": "pv", "p": p, "r": r, **({"u": u} if u else {})}  # noqa: E731
        self.visit(0, [pv("/", "https://www.google.co.uk/")])                 # Google, then a second page
        self.visit(3, [pv("/x", "https://a.example/")])
        self.visit(0, [pv("/", None, utm_source="Newsletter", utm_medium="email", utm_campaign="November Notes")],
                   ip="1.1.1.1")
        self.visit(2, [pv("/x", None, utm_source="other")], ip="1.1.1.1")   # the visit's source stays
        self.visit(0, [pv("/", "https://a.example/x")], ip="2.2.2.2")        # internal: direct, a bounce
        self.visit(0, [pv("/", "https://www.google.com/", utm_source="google", utm_medium="cpc",
                          utm_campaign="launch", click="gclid")], ip="4.4.4.4")
        self.visit(1, [pv("/", "https://bsky.app/")], ip="3.3.3.3", headers={"sec-gpc": "1"})   # opted out
        r = self.report()

        def by(kind):
            return {s["name"]: (s["value"], s["bounce_rate"]) for s in r["top"][kind]}

        self.assertEqual(by("sources"), {"Google": (2, 0.5), "newsletter": (1, 0.0), "Direct": (1, 1.0)})
        self.assertEqual(by("channels"), {"Search": (1, 0.0), "Email": (1, 0.0), "Direct": (1, 1.0),
                                          "Paid search": (1, 1.0)})
        self.assertEqual(by("campaigns"), {"november notes": (1, 0.0), "launch": (1, 1.0)})
        self.assertEqual(r["totals"]["ses"], 4)

    def test_a_consented_session_keeps_its_source(self):
        ids = {"vid": VID, "sid": SID, "sd": time.strftime("%Y-%m-%d", time.gmtime())}
        self.call("POST", "/_c/i", {**ids, "items": [{"t": "pv", "p": "/", "r": "https://news.ycombinator.com/",
                                                      "u": {"utm_campaign": "Show HN"}, "new": True}]})
        self.call.sign_in()
        s = body(self.call("GET", "/_c/api/sessions", query={"site": "a.example"}))["items"][0]
        self.assertEqual((s["ref"], s["src"], s["ch"], s["cmp"]),
                         ("news.ycombinator.com", "Hacker News", "Social", "show hn"))

    def test_a_visit_begun_by_another_request(self):
        store, W = self.app.store, store_module.web("a.example", "2026-01-01")
        at = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        self.assertEqual(store.visit("a.example", W, "abc", at, ["/"]),
                         {(W, "ses"): 1, (W, "bnc"): 1, (W, "entry#/"): 1, (W, "bnc#/"): 1, (W, "exit#/"): 1})
        real, calls = store.table.update_if_recent, []

        def late(*args, **kw):  # the first look misses the visit another request just began
            calls.append(1)
            return None if len(calls) == 1 else real(*args, **kw)

        with mock.patch.object(store.table, "update_if_recent", late):
            out = store.visit("a.example", W, "abc", at + timedelta(minutes=1), ["/x", ""])
        self.assertEqual(out, {(W, "bnc"): -1, (W, "bnc#/"): -1, (W, "exit#/"): -1, (W, "exit#/x"): 1})
        # Nothing on a counted page: the visit goes on, its exit stays.
        self.assertEqual(store.visit("a.example", W, "abc", at + timedelta(minutes=2), [""]), {})

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


def dns_answer(tid: int, qname: str, qtype: int, records, flags: int = 0x8180) -> bytes:
    out = struct.pack(">HHHHHH", tid, flags, 1, len(records), 0, 0) + intel._encode(qname) + struct.pack(">HH", qtype, 1)
    for rtype, rdata in records:
        out += b"\xc0\x0c" + struct.pack(">HHIH", rtype, 1, 300, len(rdata)) + rdata
    return out


class AddressIntel(unittest.TestCase):
    def test_dns_answers(self):
        name = intel._encode("crawl-66-249-66-1.googlebot.com")
        rcode, got = intel.parse(dns_answer(7, "1.66.249.66.in-addr.arpa", intel.PTR, [(intel.PTR, name)]))
        self.assertEqual((rcode, got), (0, [(intel.PTR, "crawl-66-249-66-1.googlebot.com")]))
        # A CNAME whose target is compressed against the question, then the address.
        rcode, got = intel.parse(dns_answer(8, "host.example.com", intel.A, [
            (intel.CNAME, b"\x03www\xc0\x0c"), (intel.A, bytes([192, 0, 2, 1])),
            (intel.AAAA, bytes(15) + b"\x01")]))
        self.assertEqual(got, [(intel.CNAME, "www.host.example.com"), (intel.A, "192.0.2.1"), (intel.AAAA, "::1")])
        self.assertEqual(intel.parse(dns_answer(9, "x.example", intel.PTR, [], flags=0x8183))[0], intel.NXDOMAIN)
        for bad in (b"short", dns_answer(1, "x.example", intel.A, [], flags=0x8380),   # truncated
                    dns_answer(1, "x.example", intel.A, [], flags=0x0100),             # a question
                    struct.pack(">HHHHHH", 1, 0x8180, 0, 1, 0, 0) + b"\xc0\x0c"):       # a pointer to itself
            with self.assertRaises(intel.DNSError):
                intel.parse(bad)

    def test_ranges(self):
        table = Ranges(build_ranges([("cloud:A", "10.0.0.0/8"), ("vpn:B", "10.1.0.0/16"), ("vpn:B", "10.1.255.0/24"),
                                     ("tor:T", "2001:db8::/32"), ("tor:T", "192.0.2.7"),
                                     ("hosting:H", "198.51.100.10-198.51.100.20"), ("x:bad", "not a range")], "t"))
        self.assertEqual(table.tags("10.1.2.3"), ("cloud:A", "vpn:B"))
        self.assertEqual(table.tags("10.2.0.1"), ("cloud:A",))
        self.assertEqual(table.tags("10.0.0.0"), ("cloud:A",))
        self.assertEqual(table.tags("10.255.255.255"), ("cloud:A",))
        self.assertEqual(table.tags("11.0.0.0"), ())
        self.assertEqual(table.tags("9.255.255.255"), ())
        self.assertEqual(table.tags("::ffff:192.0.2.7"), ("tor:T",))
        self.assertEqual(table.tags("192.0.2.8"), ())
        self.assertEqual(table.tags("2001:db8:1::1"), ("tor:T",))
        self.assertEqual(table.tags("2001:db9::1"), ())
        self.assertEqual(table.tags("198.51.100.15"), ("hosting:H",))
        self.assertEqual(table.tags("nonsense"), ())
        # Overlaps are split: 10/8, then 10.1/16 with both, then the rest of 10/8.
        self.assertEqual(table.n4, 5)
        with self.assertRaises(ValueError):
            Ranges(b"CLRANGES" + struct.pack(">I", 2) + b"{}")

    def test_kinds_and_names(self):
        self.assertEqual(Intel(tags=("cloud:AWS", "relay:iCloud Private Relay")).net("Akamai"), "relay")
        self.assertEqual(Intel(tags=("cloud:AWS", "cloud:AWS EC2 us-east-1")).service(), "AWS EC2 us-east-1")
        self.assertEqual(Intel().net("Hetzner Online GmbH"), "hosting")
        self.assertEqual(Intel().net("Comcast"), "")
        self.assertTrue(Intel(tags=("drop:Spamhaus DROP",)).listed)
        self.assertEqual(Intel(tags=("abuse:DShield",)).abuse, ("DShield",))
        for host, domain in [("c-73-1-2-3.hsd1.il.comcast.net", "comcast.net"), ("x.bb.example.co.uk", "example.co.uk"),
                             ("kd1.au-net.ne.jp", "au-net.ne.jp"), ("dns.google", "dns.google"), ("localhost", "localhost")]:
            self.assertEqual(intel.domain_of(host), domain)
        self.assertEqual(intel.canonical("::ffff:1.2.3.4"), "1.2.3.4")
        self.assertIsNone(intel.canonical("1.2.3"))

    def test_verify(self):
        google = Intel("crawl-1.googlebot.com", True, ("crawler:Googlebot",))
        self.assertEqual(verify("Googlebot", google), "verified")
        self.assertEqual(verify("Googlebot", Intel("crawl-1.googlebot.com", True)), "verified")   # by name alone
        self.assertEqual(verify("Googlebot", Intel("crawl-1.googlebot.com", False), ranges_loaded=False), "spoofed")
        self.assertEqual(verify("Googlebot", Intel("ec2-1.amazonaws.com")), "spoofed")
        self.assertEqual(verify("Googlebot", Intel(None), ranges_loaded=False), "")                 # nothing known
        self.assertEqual(verify("Googlebot", Intel(None)), "spoofed")                               # not in the list
        self.assertEqual(verify("GPTBot", Intel(None, None, ("crawler:GPTBot",))), "verified")
        self.assertEqual(verify("GPTBot", Intel(None), ranges_loaded=False), "")
        self.assertEqual(verify("Meta-ExternalAgent", Intel(), "32934", ranges_loaded=False), "verified")
        self.assertEqual(verify("Meta-ExternalAgent", Intel(), "16509", ranges_loaded=False), "spoofed")
        self.assertEqual(verify("Facebook", Intel("x.example")), "")                                # iMessage looks like it
        self.assertEqual(verify("ClaudeBot", Intel("x.example")), "")
        # Every crawler's lists have names the deploy script fetches.
        deploy = (ROOT / "infra" / "deploy.py").read_text()
        for proof in intel.PROOFS.values():
            for name in proof.ranges:
                self.assertIn(f'"crawler:{name}"', deploy)

    def test_clocks(self):
        at = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
        self.assertIs(intel.clock_elsewhere("Europe/Berlin", "America/Chicago", at), True)
        self.assertIs(intel.clock_elsewhere("America/Winnipeg", "America/Chicago", at), False)   # same offset
        self.assertIs(intel.clock_elsewhere("America/Chicago", "America/Chicago", at), False)
        self.assertIsNone(intel.clock_elsewhere("", "America/Chicago", at))
        self.assertIsNone(intel.clock_elsewhere("../../etc/passwd", "America/Chicago", at))
        self.assertIsNone(intel.clock_elsewhere("Mars/Olympus", "America/Chicago", at))

    def test_lookups_are_cached(self):
        table, calls = MemoryTable(), []
        found = fake_intel(table, calls).lookup_many(["66.249.66.1", "::ffff:198.51.100.7", "192.0.2.55", "bogus"])
        self.assertEqual(found["66.249.66.1"], Intel("crawl-66-249-66-1.googlebot.com", True, ("crawler:Googlebot",)))
        self.assertEqual(found["::ffff:198.51.100.7"].host, "c-198-51-100-7.hsd1.il.comcast.net")
        self.assertEqual(found["192.0.2.55"].host, "")
        self.assertEqual(found["bogus"], Intel())
        self.assertEqual(sorted(calls), ["192.0.2.55", "198.51.100.7", "66.249.66.1"])
        # A fresh instance (another Lambda) finds them in the table, crawler proof included.
        again = fake_intel(table, calls)
        self.assertTrue(again.lookup("66.249.66.1").confirmed)
        self.assertEqual(len(calls), 3)
        self.assertEqual(table.get("ip|198.51.100.7", "-")["host"], "c-198-51-100-7.hsd1.il.comcast.net")

    def test_no_answer_in_time(self):
        slow = Intelligence(dns=True, reverse=lambda ip, timeout: time.sleep(0.3) or "late.example")
        found = slow.lookup_many(["192.0.2.1", "192.0.2.2"], budget=0.05)
        self.assertEqual({i.host for i in found.values()}, {None})
        self.assertEqual(Intelligence().lookup("192.0.2.1"), Intel())   # DNS off: ranges only

    def test_rdap_summary(self):
        data = {"handle": "NET-66-249-64-0-1", "name": "GOOGLE", "type": "DIRECT ALLOCATION", "startAddress": "66.249.64.0",
                "endAddress": "66.249.95.255", "cidr0_cidrs": [{"v4prefix": "66.249.64.0", "length": 19}],
                "events": [{"eventAction": "registration", "eventDate": "2004-03-05T00:00:00-05:00"}],
                "remarks": [{"description": ["Google's crawlers"]}],
                "entities": [{"roles": ["registrant"], "vcardArray": ["vcard", [["fn", {}, "text", "GOOGLE-MNT"],
                                                                              ["kind", {}, "text", "individual"]]]},
                             {"roles": ["registrant"], "vcardArray": ["vcard", [["fn", {}, "text", "Google LLC"],
                                                                              ["kind", {}, "text", "org"]]],
                              "entities": [{"roles": ["abuse"], "vcardArray": ["vcard", [
                                  ["fn", {}, "text", "Abuse"], ["email", {}, "text", "network-abuse@google.com"]]]}]}]}
        got = intel.summarize_rdap(data, "rdap.arin.net")
        self.assertEqual((got["holder"], got["abuse"], got["cidrs"], got["registered"][:4], got["remarks"]),
                         ("Google LLC", "network-abuse@google.com", ["66.249.64.0/19"], "2004", ["Google's crawlers"]))

    @unittest.skipUnless(os.environ.get("INTEL_RANGES"), "set INTEL_RANGES to test a built geo/ranges.bin")
    def test_built_table(self):
        table = Ranges.open(os.environ["INTEL_RANGES"])
        self.assertIn("crawler:Googlebot", table.tags("66.249.66.1"))
        self.assertTrue(any(t.startswith("cloud:AWS") for t in table.tags("3.80.0.1")))


class Enrichment(unittest.TestCase):
    """Addresses as the store keeps them: names, connections, crawlers checked."""

    def setUp(self):
        self.app = make(with_intel=True)
        self.call = Client(self.app)
        self.call.sign_in()

    def report(self, site="a.example"):
        return body(self.call("GET", "/_c/api/report", query={"site": site, "days": "1"}))

    def test_server_logs(self):
        now = datetime.now(timezone.utc)
        google = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"

        def line(ip, ua, host="a.example", path="/"):
            return LogLine(now, host, ip, "GET", path, "", 200, "", ua)
        self.app.store.ingest_log([
            line("66.249.66.1", google), line("52.4.1.1", google), line("52.4.9.9", google),
            line("172.224.1.1", SAFARI), line("198.51.100.7", SAFARI), line("1.10.16.5", SAFARI),
            line("52.4.2.2", SAFARI), line("203.0.113.9", SAFARI), line("198.51.100.8", SAFARI, "b.example")])
        r = self.report()
        self.assertEqual(r["top"]["verified"], [{"name": "Googlebot", "value": 1}])
        self.assertEqual(r["top"]["impostors"], [{"name": "Googlebot", "value": 2}])
        self.assertEqual({b["name"] for b in r["top"]["bots"]}, {"Googlebot", "Googlebot (fake)", "Spamhaus DROP"})
        self.assertEqual({c["name"]: c["value"] for c in r["top"]["server_connections"]},
                         {"relay": 1, "isp": 2, "hosting": 1})
        self.assertEqual((r["totals"]["dc"], r["totals"]["abuse"]), (1, 1))
        self.assertIn({"name": "comcast.net", "value": 1}, r["top"]["server_domains"])
        self.assertIn({"name": "googlebot.com", "value": 2}, r["top"]["bot_domains"])
        people = body(self.call("GET", "/_c/api/log", query={"site": "a.example", "kind": "hum"}))["items"]
        by_ip = {p["ip"]: p for p in people}
        self.assertEqual(by_ip["198.51.100.7"]["host"], "c-198-51-100-7.hsd1.il.comcast.net")
        self.assertEqual((by_ip["172.224.1.1"]["net"], by_ip["172.224.1.1"]["svc"], by_ip["172.224.1.1"]["dc"]),
                         ("relay", "iCloud Private Relay", 0))
        self.assertEqual(by_ip["203.0.113.9"]["abuse"], "blocklist.de")
        bots = body(self.call("GET", "/_c/api/log", query={"site": "a.example", "kind": "bot"}))["items"]
        self.assertEqual({b["ip"]: b.get("ver") for b in bots},
                         {"66.249.66.1": "verified", "52.4.1.1": "spoofed", "52.4.9.9": "spoofed", "1.10.16.5": None})
        # A site that truncates addresses gets no reverse DNS: names spell addresses out.
        other = body(self.call("GET", "/_c/api/log", query={"site": "b.example", "kind": "hum"}))["items"]
        self.assertNotIn("host", other[0])

    def test_beacons(self):
        ids = {"vid": VID, "sid": SID, "sd": time.strftime("%Y-%m-%d", time.gmtime())}
        chicago = {"cloudfront-viewer-time-zone": "America/Chicago", "cloudfront-viewer-country-region-name": "Illinois"}
        self.call("POST", "/_c/i", {**ids, "tz": "Europe/Berlin", "items": [{"t": "pv", "p": "/", "new": True}]},
                  ip="198.51.100.7", headers=chicago)
        self.call("POST", "/_c/i", {"tz": "America/Chicago", "items": [{"t": "pv", "p": "/"}]},
                  ip="172.224.9.9", headers=chicago)
        r = self.report()
        self.assertEqual(r["totals"]["tzx"], 1)
        self.assertEqual({c["name"]: c["value"] for c in r["top"]["connections"]}, {"isp": 1, "relay": 1})
        view = body(self.call("GET", "/_c/api/log", query={"site": "a.example", "kind": "pv"}))["items"][0]
        self.assertEqual((view["host"], view["region"], view["tz"], view["tzx"]),
                         ("c-198-51-100-7.hsd1.il.comcast.net", "Illinois", "Europe/Berlin", 1))
        session = body(self.call("GET", "/_c/api/sessions", query={"site": "a.example"}))["items"][0]
        self.assertEqual(session["host"], "c-198-51-100-7.hsd1.il.comcast.net")

    def test_one_address(self):
        with mock.patch.object(intel, "whois", return_value={"holder": "Google LLC"}) as whois:
            got = body(self.call("GET", "/_c/api/ip", query={"ip": "66.249.66.1"}))
            self.assertEqual((got["host"], got["confirmed"], got["net"], got["service"], got["whois"]),
                             ("crawl-66-249-66-1.googlebot.com", True, "crawler", "Googlebot", {"holder": "Google LLC"}))
            # Any name is checked forward here, not just crawlers'.
            self.assertIs(body(self.call("GET", "/_c/api/ip", query={"ip": "198.51.100.7"}))["confirmed"], True)
            private = body(self.call("GET", "/_c/api/ip", query={"ip": "10.0.0.1"}))
            self.assertEqual((private["global"], "whois" in private), (False, False))
            self.assertEqual(whois.call_count, 1)   # 198.51.100.7 is a documentation address: not global
        self.assertEqual(self.call("GET", "/_c/api/ip", query={"ip": "nope"}).status, 400)
        self.call.cookies.clear()
        self.assertEqual(self.call("GET", "/_c/api/ip", query={"ip": "66.249.66.1"}).status, 401)

    def test_who(self):
        """What a page hears when it asks about its visitor (c.js's cl("who"))."""
        calls: list[str] = []
        self.app.store.intel = fake_intel(self.app.store.table, calls)
        google = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"

        def who(ip, ua=SAFARI, query=None, headers=None):
            res = self.call("GET", "/_c/who", ip=ip, ua=ua, query=query, headers=headers)
            self.assertEqual((res.status, res.headers["Cache-Control"]), (200, "no-store"))
            return body(res)

        chicago = {"cloudfront-viewer-time-zone": "America/Chicago"}
        self.assertEqual(who("198.51.100.7", query={"tz": "Europe/Berlin"}, headers=chicago), {
            "bot": None, "net": "isp", "service": "", "listed": False, "abuse": [], "elsewhere": True,
            "country": "US", "asn": "", "network": ""})
        self.assertIs(who("198.51.100.7", query={"tz": "America/Chicago"}, headers=chicago)["elsewhere"], False)
        self.assertEqual({ip: (w["net"], w["service"]) for ip in ("172.224.1.1", "52.4.2.2", "2001:db8:7::1")
                          for w in [who(ip)]},
                         {"172.224.1.1": ("relay", "iCloud Private Relay"), "52.4.2.2": ("hosting", "AWS EC2 us-east-1"),
                          "2001:db8:7::1": ("tor", "Tor exit")})
        self.assertEqual(who("203.0.113.9")["abuse"], ["blocklist.de"])
        dropped = who("1.10.16.5")
        self.assertEqual((dropped["bot"], dropped["listed"]),
                         ({"name": "Spamhaus DROP", "category": "scanner", "verified": None}, True))
        # Crawlers are checked: by their published ranges, else by a name that resolves back.
        real = who("66.249.66.1", google)
        self.assertEqual((real["bot"], real["net"]),
                         ({"name": "Googlebot", "category": "search", "verified": True}, "crawler"))
        self.assertIs(who("52.4.1.1", google)["bot"]["verified"], False)
        self.assertIs(who("52.4.9.9", google)["bot"]["verified"], False)   # named like Googlebot, isn't
        self.assertEqual(who("198.51.100.7", query={"wd": "1"})["bot"],
                         {"name": "webdriver", "category": "automation", "verified": None})
        self.assertEqual(who("198.51.100.7", ua="")["bot"]["name"], "(no user agent)")
        self.assertIsNone(who("198.51.100.7", headers={"dnt": "1"})["bot"])
        # Reverse DNS only for crawler claims the ranges didn't settle; nothing counted or kept but that.
        self.assertEqual(sorted(calls), ["52.4.1.1", "52.4.9.9"])
        self.assertEqual([pk for pk in self.app.store.table.items if not pk.startswith("ip|")], [])
        self.assertEqual(self.call("GET", "/_c/who", site="nope.example").status, 404)
        self.assertEqual(self.call("GET", "/_c/who", site=None).status, 404)


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


FUNNEL_DATA = {**DATA, "site": [
    {**DATA["site"][0], "funnel": [
        {"name": "Sign-up", "steps": ["/", "/pricing/", "event:sign_up?plan=pro"]},
        {"name": "Returning", "by": "visitor", "steps": ["/", "/pricing"]},
    ]},
    DATA["site"][1],
]}
SID2 = "22222222-2222-4333-8444-555555555555"
SID3 = "33333333-2222-4333-8444-555555555555"
SID4 = "44444444-2222-4333-8444-555555555555"
VID2 = "bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee"


class FunnelConfig(unittest.TestCase):
    def test_consent_docs(self):
        site = {"id": "a.example", "url": "https://a.example/", "consent_docs": "/privacy/v{version}"}
        cfg = Config.from_data({"site": [site]})
        self.assertEqual(cfg.sites["a.example"].consent_docs_url, "https://a.example/privacy/v{version}")
        self.assertEqual(Config.from_data(cfg.to_data()).sites["a.example"].consent_docs, "/privacy/v{version}")
        for bad in ("privacy/v{version}", "/privacy", "https://a.example/privacy/v{version}"):
            with self.assertRaises(ValueError):
                Config.from_data({"site": [{**site, "consent_docs": bad}]})

    def test_steps(self):
        page = Step.parse("/pricing/")
        self.assertTrue(page.matches("page", "/pricing"))
        self.assertFalse(page.matches("page", "/pricing/team"))
        self.assertFalse(page.matches("event", "/pricing"))
        self.assertTrue(Step.parse("/docs/index.html").matches("page", "/docs"))
        under = Step.parse("/blog/*")
        self.assertTrue(under.matches("page", "/blog/a/b"))
        self.assertFalse(under.matches("page", "/blog"))
        event = Step.parse("event:sign_up?plan=pro&trial=true")
        self.assertTrue(event.matches("event", "sign_up", {"plan": "pro", "trial": True, "x": 1}))
        self.assertFalse(event.matches("event", "sign_up", {"plan": "pro"}))
        self.assertFalse(event.matches("event", "sign_up_now", {"plan": "pro", "trial": True}))
        self.assertTrue(Step.parse("event:value?value=3").matches("event", "value", {"value": 3.0}))
        self.assertTrue(Step.parse("event:video_*").matches("event", "video_play"))
        for bad in ("pricing", "", "event:", "event:a/b", "/x?y=1", 7):
            with self.assertRaises(ValueError, msg=bad):
                Step.parse(bad)

    def test_funnels(self):
        cfg = Config.from_data(FUNNEL_DATA)
        funnels = cfg.sites["a.example"].funnels
        self.assertEqual([(f.name, f.by, len(f.steps)) for f in funnels], [("Sign-up", "session", 3), ("Returning", "visitor", 2)])
        # Lambda gets them through the deploy script's JSON.
        again = Config.from_data(json.loads(json.dumps(cfg.to_data())))
        self.assertEqual(again.sites["a.example"].funnels, funnels)
        self.assertEqual(again.sites["b.example"].funnels, ())
        for bad in ({"name": "x", "steps": ["/"]}, {"steps": ["/", "/a"]}, {"name": "x", "steps": ["/", "a"]},
                    {"name": "x", "by": "user", "steps": ["/", "/a"]}, {"name": "x", "steps": ["/"] * 11}):
            with self.assertRaises(ValueError, msg=bad):
                Config.from_data({"site": [{"id": "a.example", "funnel": [bad]}]})
        with self.assertRaises(ValueError):
            Config.from_data({"site": [{"id": "a.example", "funnel": [{"name": "x", "steps": ["/", "/a"]}] * 2}]})


class Funnels(unittest.TestCase):
    def setUp(self):
        cfg = Config.from_data(FUNNEL_DATA, session_secret=SECRET, origin_secret="origin", server_secret="server")
        self.app = App(cfg, Store(cfg, MemoryTable(), MemoryBlobs()))
        self.call = Client(self.app)
        self.call.sign_in()
        self.start = store_module.now() - timedelta(hours=3)

    def at(self, minute: int):
        at = self.start + timedelta(minutes=minute)
        return mock.patch.object(store_module, "now", lambda: at)

    def send(self, minute, items, sid=SID, vid=VID, uid=None):
        ids = {"sid": sid, "vid": vid, "sd": self.start.strftime("%Y-%m-%d"), **({"uid": uid} if uid else {})}
        with self.at(minute):
            self.call("POST", "/_c/i", {**(ids if sid else {}), "items": items})

    def funnels(self, **query):
        res = self.call("GET", "/_c/api/funnels", query={"site": "a.example", "days": "7", **query})
        return res.status, body(res)

    def test_steps_in_order(self):
        pv = lambda p: {"t": "pv", "p": p}  # noqa: E731
        pro = {"t": "ev", "n": "sign_up", "props": {"plan": "pro"}}
        self.send(0, [pv("/")])                                   # SID: all three, a minute apart
        self.send(1, [pv("/pricing")])
        self.send(2, [pro])
        self.send(0, [pv("/pricing"), pv("/")], sid=SID2)         # pricing before home: step 1 only
        self.send(1, [{"t": "ev", "n": "sign_up", "props": {"plan": "free"}}], sid=SID2)
        self.send(0, [pv("/"), pv("/pricing/"), pro], sid=SID3)  # one batch, in order
        self.send(0, [pro, pv("/pricing"), pv("/")], sid=SID4)   # one batch, backwards: step 1 only
        self.send(0, [pv("/"), pv("/pricing"), pro], sid=None)   # anonymous: not in any funnel
        status, d = self.funnels()
        self.assertEqual(status, 200)
        signup = d["funnels"][0]
        self.assertEqual(signup["name"], "Sign-up")
        self.assertEqual([s["count"] for s in signup["steps"]], [4, 2, 2])
        self.assertEqual((signup["entered"], signup["completed"], d["sessions"]), (4, 2, 4))
        self.assertEqual(signup["steps"][1]["median_s"], 30.0)    # 60 s and 0 s
        self.assertEqual({x["sid"] for x in signup["steps"][0]["dropped"]}, {SID2, SID4})
        self.assertEqual(d["configured"][0]["steps"], ["/", "/pricing/", "event:sign_up?plan=pro"])

    def test_by_visitor_spans_sessions(self):
        self.send(0, [{"t": "pv", "p": "/"}], sid=SID)
        self.send(30, [{"t": "pv", "p": "/pricing"}], sid=SID2)
        self.send(0, [{"t": "pv", "p": "/pricing"}], sid=SID3, vid=VID2)
        _, d = self.funnels()
        returning = d["funnels"][1]
        self.assertEqual([s["count"] for s in returning["steps"]], [1, 1])
        self.assertEqual(d["visitors"], 2)
        _, d = self.funnels(steps=json.dumps(["/", "/pricing"]), by="session")
        self.assertEqual([s["count"] for s in d["funnels"][0]["steps"]], [1, 0])

    def test_backend_events_join_the_users_session(self):
        key = auth.server_key("server", "a.example")

        def backend(minute, uid):
            with self.at(minute):
                self.call("POST", "/_c/server/events", {"events": [{"name": "paid", "uid": uid}]},
                          headers={"x-carterlitics-key": key})

        backend(0, "u1")                                           # before the session began
        self.send(1, [{"t": "pv", "p": "/"}], uid="u1")
        self.send(2, [{"t": "pv", "p": "/"}], sid=SID2, vid=VID2)  # never identified
        backend(5, "u1")
        backend(5, "u2")                                           # no session to join
        # Had the minute-0 event joined, it would come before the page view: [1, 1].
        _, d = self.funnels(steps=json.dumps(["event:paid", "/"]))
        self.assertEqual([s["count"] for s in d["funnels"][0]["steps"]], [1, 0])
        _, d = self.funnels(steps=json.dumps(["/", "event:paid"]))
        self.assertEqual([s["count"] for s in d["funnels"][0]["steps"]], [2, 1])

    def test_explore_needs_good_steps(self):
        for steps in ("not json", json.dumps(["/"]), json.dumps(["/", "pricing"]), json.dumps({"a": 1})):
            status, d = self.funnels(steps=steps)
            self.assertEqual(status, 400, steps)
            self.assertIn("error", d)
        self.call.cookies.clear()
        self.assertEqual(self.funnels()[0], 401)


class Page:
    """c.js in a stub page (tests/client_harness.js), and what it sent."""

    def page(self, before="", after="", **spec):
        out = subprocess.run(["node", str(ROOT / "tests" / "client_harness.js"),
                              json.dumps({"before": before, "after": after, **spec})],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)


@unittest.skipUnless(shutil.which("node"), "needs node to run c.js")
class GoogleCalls(Page, unittest.TestCase):
    """Google's tracking calls become events."""

    @staticmethod
    def events(out):
        return [(i["n"], i["props"]) for i in out["items"] if i["t"] == "ev"]

    def test_gtag_and_the_data_layer(self):
        out = self.page(
            before="""window.dataLayer = window.dataLayer || [];
                      function gtag() { dataLayer.push(arguments); }
                      gtag('js', new Date()); gtag('config', 'G-TEST');
                      gtag('event', 'sign_up', { method: 'google', items: [{ id: 1 }],
                                                 event_callback: function () { log.push('sent'); } });""",
            after="""gtag('event', 'purchase', { value: 9.5, currency: 'USD', send_to: 'G-TEST' });
                     gtag('event', 'page_view', { page_path: '/start' });
                     gtag('event', 'page_view', { page_location: 'https://a.example/checkout?step=2' });
                     dataLayer.push({ event: 'gtm.click' }, { event: 'newsletter', list: 'weekly' });
                     dataLayer.push(['event', 'not_gtag']);
                     gtag('event', '¡Add to cart!');""")
        self.assertEqual(self.events(out), [
            ("sign_up", {"method": "google", "items": '[{"id":1}]'}),
            ("purchase", {"value": 9.5, "currency": "USD"}),
            ("newsletter", {"list": "weekly"}),
            ("Add to cart", {}),
        ])
        self.assertEqual([i["p"] for i in out["items"] if i["t"] == "pv"], ["/start", "/checkout"])
        self.assertEqual(out["log"], ["sent"])
        # And the collector takes every one of them.
        call = Client(make())
        self.assertEqual(call("POST", "/_c/i", {"items": out["items"]}).status, 204)
        call.sign_in()
        report = body(call("GET", "/_c/api/report", query={"site": "a.example", "days": "1"}))
        self.assertEqual({e["name"] for e in report["top"]["events"]},
                         {"sign_up", "purchase", "newsletter", "Add to cart"})

    def test_analytics_js(self):
        out = self.page(
            before="""window.ga = window.ga || function () { (ga.q = ga.q || []).push(arguments); }; ga.l = +new Date;
                      ga('create', 'UA-1-1', 'auto'); ga('send', 'pageview');
                      ga('send', 'event', 'Videos', 'play', 'intro', 3);""",
            after="""ga('send', { hitType: 'event', eventCategory: 'Outbound', eventAction: 'click',
                                  eventLabel: 'https://x.example', hitCallback: function () { log.push('hit'); } });
                     ga('t2.send', 'event', 'Q&A', 'open/close', { nonInteraction: true });
                     ga('set', 'page', '/virtual'); ga('send', 'pageview');
                     ga('require', 'ec'); ga('ec:addProduct', { id: 'p1' });""",
            probe="ga.q.length")
        self.assertEqual(self.events(out), [
            ("play", {"event_category": "Videos", "event_label": "intro", "value": 3}),
            ("click", {"event_category": "Outbound", "event_label": "https://x.example"}),
            ("open_close", {"event_category": "Q&A", "non_interaction": True}),
        ])
        self.assertEqual([i["p"] for i in out["items"] if i["t"] == "pv"], ["/start", "/virtual"])
        self.assertEqual(out["log"], ["hit"])
        self.assertEqual(out["probe"], 9)   # every call still queued for analytics.js, should it load

    def test_ga_js_async_queue(self):
        out = self.page(
            before="""var _gaq = _gaq || [];
                      _gaq.push(['_setAccount', 'UA-1-1'], ['_trackPageview']);
                      _gaq.push(['_trackEvent', 'Signup', 'submit', 'footer']);""",
            after="""_gaq.push(['_set', 'hitCallback', function () { log.push('gaq'); }],
                               ['b._trackEvent', 'Video', 'pause', undefined, 7, true]);
                     _gaq.push(['_trackEvent', 'Video']);
                     _gaq.push(function () { log.push('ga.js only'); });""")
        self.assertEqual(self.events(out), [
            ("submit", {"event_category": "Signup", "event_label": "footer"}),
            ("pause", {"event_category": "Video", "value": 7, "non_interaction": True}),
        ])
        self.assertEqual(out["log"], ["gaq"])

    def test_google_runs_its_own_callbacks(self):
        out = self.page(
            before="window.google_tag_manager = {}; window.dataLayer = []; function gtag() { dataLayer.push(arguments); }",
            after="""gtag('event', 'x', { event_callback: function () { log.push('once'); } });
                     var cb = dataLayer[dataLayer.length - 1][2].event_callback; cb(); cb();""")
        self.assertEqual(self.events(out), [("x", {})])
        self.assertEqual(out["log"], ["once"])

    def test_off_and_opted_out(self):
        out = self.page(dataset={"google": "off"},
                        before="window.dataLayer = []; function gtag() { dataLayer.push(arguments); }",
                        after="gtag('event', 'x', {});", probe="typeof window.ga + ' ' + typeof window._gaq")
        self.assertEqual((self.events(out), out["probe"]), ([], "undefined undefined"))
        # Do Not Track: nothing is sent, but the page's callback still runs (it may navigate).
        out = self.page(dnt=True, after="gtag('event', 'x', { event_callback: function () { log.push('go'); } });")
        self.assertEqual((out["items"], out["log"]), ([], ["go"]))

    def test_user_id_identifies_a_consented_visitor(self):
        out = self.page(cookies={"consent": "all"},
                        after="gtag('config', 'G-TEST', { user_id: 'user-9' }); gtag('event', 'x');")
        self.assertEqual(out["envelopes"][-1]["uid"], "user-9")
        out = self.page(after="gtag('config', 'G-TEST', { user_id: 'user-9' }); gtag('event', 'x');")
        self.assertNotIn("uid", out["envelopes"][-1])


@unittest.skipUnless(shutil.which("node"), "needs node to run c.js")
class ClientCampaignTags(Page, unittest.TestCase):
    """The landing address's campaign tags go with its page view; ad click ids by name only."""

    @staticmethod
    def views(out):
        return [i for i in out["items"] if i["t"] == "pv"]

    def test_tags_and_click_ids(self):
        out = self.page(url="https://a.example/start?utm_source=Newsletter&utm_campaign=nov&utm_term=x"
                            "&gclid=SECRET-CLICK&fbclid=abc&ref=" + "r" * 150,
                        referrer="https://www.google.com/")
        pv, = self.views(out)
        self.assertEqual(pv["r"], "https://www.google.com/")
        self.assertEqual(pv["u"], {"utm_source": "Newsletter", "utm_campaign": "nov", "ref": "r" * 100,
                                   "click": "gclid"})
        self.assertNotIn("SECRET-CLICK", json.dumps(out["posts"]))

    def test_an_untagged_address_sends_none(self):
        pv, = self.views(self.page(url="https://a.example/start?page=2"))
        self.assertNotIn("u", pv)


@unittest.skipUnless(shutil.which("node"), "needs node to run c.js")
class ClientSessions(Page, unittest.TestCase):
    """c.js's sessions end after 30 minutes without a page view or event."""
    OLD = "22222222-2222-4222-8222-222222222222"
    MIN = 60 * 1000
    CONSENTED = {"consent": "all", "vid": VID}   # a returning visitor: the first new id is the session's

    @staticmethod
    def sent(out):
        """(type, sid, new) for each page view and event, in the order they went."""
        return [(i["t"], p.get("sid"), i.get("new", False))
                for p in out["posts"] for i in p.get("items", []) if i["t"] in ("pv", "ev")]

    def test_idle_minutes_end_a_session(self):
        out = self.page(cookies=self.CONSENTED, probe="sessionStorage.getItem('cl_sid')",
                        after="advance(31 * 60 * 1000); cl('track', 'outbound', {});")
        (pv, first, new1), (ev, second, new2) = self.sent(out)
        self.assertEqual((pv, ev, new1, new2), ("pv", "ev", True, True))
        self.assertEqual(first, SID)
        self.assertNotEqual(second, SID)
        self.assertEqual(out["probe"], second)

    def test_activity_keeps_it_going(self):
        out = self.page(cookies=self.CONSENTED, after=(
            "advance(29 * 60 * 1000); cl('track', 'a', {}); advance(29 * 60 * 1000); cl('page', '/next');"
            "advance(40 * 60 * 1000); cl('track', 'seen', { non_interaction: true });"))
        self.assertEqual({sid for _, sid, _ in self.sent(out)}, {SID})
        self.assertEqual([new for _, _, new in self.sent(out)], [True, False, False, False])

    def test_a_page_load_after_idle_minutes(self):
        today = time.strftime("%Y-%m-%d", time.gmtime())
        now = int(time.time() * 1000)
        for at, same in ((now - 5 * self.MIN, True), (now - 31 * self.MIN, False), (None, False)):
            stored = {"cl_sid": self.OLD, "cl_sd": today, **({"cl_at": str(at)} if at else {})}
            (kind, sid, new), = self.sent(self.page(cookies=self.CONSENTED, session=stored))
            self.assertEqual((kind, sid == self.OLD, new), ("pv", same, not same), at)


@unittest.skipUnless(shutil.which("node"), "needs node to run c.js")
class ClientIdentify(Page, unittest.TestCase):
    """cl("identify"): a consented visitor's user id, and forgetting it."""

    def test_waits_in_memory_for_consent_on_the_page(self):
        out = self.page(after="cl('identify', 'google:42'); cl('track', 'x', {});",
                        probe="sessionStorage.getItem('cl_uid')")
        self.assertEqual(([e.get("uid") for e in out["envelopes"]], out["probe"]), ([None], None))
        out = self.page(after="cl('identify', 'google:42'); cl('consent', 'all');",
                        probe="sessionStorage.getItem('cl_uid')")
        self.assertEqual((out["envelopes"][-1]["uid"], out["probe"]), ("google:42", "google:42"))

    def test_signing_out_forgets_it(self):
        out = self.page(cookies={"consent": "all", "vid": VID}, probe="sessionStorage.getItem('cl_uid')", after="""
            cl('identify', 'google:42');
            carterlitics.flush().then(function () {
              cl('identify', null); cl('track', 'after', {}); carterlitics.flush(); });""")
        self.assertEqual([e.get("uid") for e in out["envelopes"]], ["google:42", None])
        self.assertIsNone(out["probe"])
        # A later page in the tab doesn't bring it back.
        out = self.page(cookies={"consent": "all", "vid": VID}, session={"cl_uid": "google:42"},
                        after="cl('identify', null);")
        self.assertNotIn("uid", out["envelopes"][-1])


@unittest.skipUnless(shutil.which("node"), "needs node to run c.js")
class ClientConsentVersions(Page, unittest.TestCase):
    """data-consent-version: an Accept given under an earlier version is asked for again."""
    BANNER = """var banner = { hidden: true, addEventListener: function () {}, querySelector: function () { return null; } };
                document.querySelector = function (s) { return s === '[data-consent]' ? banner : null; };"""
    V2 = {"consentVersion": "2"}

    def visit(self, consent, dataset=V2, after=""):
        """(banner hidden, cl("consent"), identified, cookies) for a page load with that cookie."""
        out = self.page(before=self.BANNER, after=after, dataset=dataset, cookies={"consent": consent, "vid": VID},
                        probe="JSON.stringify([banner.hidden, carterlitics.consent(), document.cookie])")
        hidden, choice, cookies = json.loads(out["probe"])
        return hidden, choice, "vid" in out["envelopes"][-1], cookies

    def test_an_earlier_accept_is_asked_again(self):
        self.assertEqual(self.visit("all")[:3], (False, None, False))
        # Accepting again records the version, and the next page doesn't ask.
        self.assertIn("consent=all.2", self.visit("all", after="cl('consent', 'all');")[3])
        self.assertEqual(self.visit("all.2")[:3], (True, "all", True))

    def test_anonymous_only_stands_and_version_1_is_unchanged(self):
        self.assertEqual(self.visit("essential")[:3], (True, "essential", False))
        self.assertEqual(self.visit("all", dataset={})[:3], (True, "all", True))
        self.assertEqual(self.visit("all.3")[:3], (True, "all", True))   # a later version covers this one
        self.assertEqual(self.visit("all.x")[:3], (False, None, False))   # not a choice at all

    def test_what_they_send_says_which_version_they_accepted(self):
        def sent(**spec):
            return self.page(**spec)["envelopes"][-1]
        self.assertEqual(sent(dataset=self.V2, cookies={"consent": "all.2", "vid": VID})["cv"], 2)
        self.assertEqual(sent(cookies={"consent": "all", "vid": VID})["cv"], 1)
        full = sent(dataset={"mode": "full"})   # nobody was asked: no version
        self.assertEqual(("vid" in full, "cv" in full), (True, False))


@unittest.skipUnless(shutil.which("node"), "needs node to run c.js")
class ClientWho(Page, unittest.TestCase):
    """cl("who"): what the collector makes of this visitor, when the page asks."""
    VERDICT = {"bot": None, "net": "vpn", "service": "Mullvad"}

    def test_asked_once_a_page_and_only_when_wanted(self):
        self.assertEqual(self.page(answers=[self.VERDICT])["gets"], [])
        out = self.page(answers=[self.VERDICT], probe="Intl.DateTimeFormat().resolvedOptions().timeZone", after="""
            cl('who').then(function (w) { log.push(w.net); });
            carterlitics.who(function (w) { log.push(w.service); });""")
        self.assertEqual(out["log"], ["vpn", "Mullvad"])
        self.assertEqual(out["gets"], ["/_c/who?tz=" + urllib.parse.quote(out["probe"], safe="")])

    def test_through_the_stub(self):
        out = self.page(answers=[self.VERDICT], before="""
            window.cl = window.cl || function () { (cl.q = cl.q || []).push(arguments); };
            cl('who', function (w) { log.push(w.net); });""")
        self.assertEqual(out["log"], ["vpn"])

    def test_webdriver_says_so(self):
        out = self.page(answers=[self.VERDICT], before="navigator.webdriver = true;", after="cl('who');")
        self.assertTrue(out["gets"][0].endswith("&wd=1"), out["gets"])

    def test_unreachable_is_null_and_asked_again(self):
        out = self.page(answers=[None, 503, self.VERDICT], after="""
            cl('who').then(function (a) { log.push(a);
              cl('who').then(function (b) { log.push(b);
                cl('who').then(function (c) { log.push(c.net); }); }); });""")
        self.assertEqual((out["log"], len(out["gets"])), ([None, None, "vpn"], 3))


@unittest.skipUnless(os.environ.get("GEO_MMDB"), "set GEO_MMDB to test against a real database")
class Geo(unittest.TestCase):
    def test_lookup(self):
        geo = Geolocator.from_env()
        self.assertEqual(geo.lookup("8.8.8.8").asn, "15169")
        self.assertEqual(geo.lookup("192.168.1.1").asn, "")
        self.assertEqual(geo.lookup("not an ip").country, "")


if __name__ == "__main__":
    unittest.main()
