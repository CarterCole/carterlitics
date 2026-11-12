"""Search Console: which property is a site's, the report, and signing in to Google.

    python3 -m unittest discover tests
"""
from __future__ import annotations

import gzip
import json
import re
import sys
import time
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from carterlitics import app as app_module  # noqa: E402
from carterlitics import google  # noqa: E402
from carterlitics.app import App  # noqa: E402
from carterlitics.backends import MemoryBlobs, MemoryTable  # noqa: E402
from carterlitics.config import Config  # noqa: E402
from carterlitics.google import GoogleError, SearchConsole, Tokens  # noqa: E402
from carterlitics.store import Store  # noqa: E402
from carterlitics.web import Request  # noqa: E402

DATA = {
    "console": "a.example",
    "google": {"project": "proj"},
    "site": [
        {"id": "a.example", "url": "https://a.example", "aliases": ["www.a.example"]},
        {"id": "blog.a.example", "url": "https://blog.a.example", "aliases": ["blog.b.example"]},
        {"id": "c.example", "url": "https://c.example"},
        {"id": "*.a.example", "url": "https://a.example", "aliases": ["*.a.example"]},
    ],
}
CFG = Config.from_data(DATA, session_secret="s" * 32)
SITES = CFG.sites
NOW = datetime(2026, 11, 8, 20, 0, tzinfo=timezone.utc)    # Nov 8 in Pacific time


class FakeGoogle:
    """Search Console's API, from canned rows per (property, dimension). Records each call."""

    def __init__(self, properties: dict[str, str], rows: dict | None = None, sitemaps: dict | None = None):
        self.properties = properties
        self.rows = rows or {}
        self.sitemaps = sitemaps or {}
        self.calls: list[tuple[str, str, dict | None]] = []

    def request(self, method, url, scope=google.READ, body=None):
        path = urllib.parse.urlsplit(url).path
        prop = urllib.parse.unquote(path.split("/sites/")[1].split("/")[0]) if "/sites/" in path else ""
        self.calls.append((method, prop or path.rsplit("/", 1)[-1], body))
        if path.endswith("/sites"):
            return {"siteEntry": [{"siteUrl": k, "permissionLevel": v} for k, v in self.properties.items()]}
        if path.endswith("/sitemaps"):
            return {"sitemap": self.sitemaps.get(prop, [])}
        return {"rows": self.rows.get((prop, body["dimensions"][0]), [])}

    def bodies(self, prop):
        return {c[2]["dimensions"][0]: c[2] for c in self.calls if c[1] == prop and c[2]}


def row(key, clicks, impressions, position):
    return {"keys": [key], "clicks": clicks, "impressions": impressions,
            "ctr": clicks / impressions if impressions else 0, "position": position}


class Properties(unittest.TestCase):
    def test_hosts(self):
        self.assertEqual(google.hosts_of(SITES["a.example"]), ["a.example", "www.a.example"])
        self.assertEqual(google.hosts_of(SITES["blog.a.example"]), ["blog.a.example", "blog.b.example"])
        self.assertEqual(google.hosts_of(SITES["*.a.example"]), [])     # a catch-all has no pages of its own

    def test_most_specific_property(self):
        props = ["sc-domain:example", "sc-domain:a.example", "https://c.example/", "http://d.example/"]
        self.assertEqual(google.property_for("blog.a.example", props), "sc-domain:a.example")
        self.assertEqual(google.property_for("a.example", props), "sc-domain:a.example")
        self.assertEqual(google.property_for("c.example", props), "https://c.example/")
        self.assertEqual(google.property_for("d.example", props), "http://d.example/")
        self.assertEqual(google.property_for("e.example", props), "sc-domain:example")
        self.assertIsNone(google.property_for("a.example.org", props))
        self.assertIsNone(google.property_for("xa.example", ["sc-domain:a.example"]))

    def test_a_site_across_two_domains(self):
        self.assertEqual(google.plan(SITES["blog.a.example"], ["sc-domain:a.example", "sc-domain:b.example"]),
                         [("sc-domain:a.example", ["blog.a.example"]), ("sc-domain:b.example", ["blog.b.example"])])
        self.assertEqual(google.plan(SITES["blog.a.example"], ["sc-domain:a.example"]),
                         [("sc-domain:a.example", ["blog.a.example"])])

    def test_page_filter(self):
        self.assertEqual(google.page_filter("https://c.example/", ["c.example"]), [])
        [group] = google.page_filter("sc-domain:a.example", ["a.example", "www.a.example"])
        [f] = group["filters"]
        self.assertEqual((f["dimension"], f["operator"]), ("page", "includingRegex"))
        pattern = re.compile(f["expression"])
        for url in ("https://a.example/", "https://www.a.example/x?y", "http://a.example", "https://a.example:8443/p"):
            self.assertTrue(pattern.search(url), url)
        for url in ("https://blog.a.example/", "https://a.example.evil/", "https://aXexample/", "https://xa.example/"):
            self.assertFalse(pattern.search(url), url)


class Report(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGoogle(
            {"sc-domain:a.example": "siteRestrictedUser", "sc-domain:b.example": "siteOwner",
             "https://unverified.example/": "siteUnverifiedUser"},
            rows={("sc-domain:a.example", "date"): [row("2026-11-05", 2, 40, 3.0), row("2026-11-06", 1, 10, 8.0),
                                                    row("2026-10-30", 9, 90, 1.0)],
                  ("sc-domain:b.example", "date"): [row("2026-11-06", 1, 30, 4.0)],
                  ("sc-domain:a.example", "query"): [row("blog", 3, 50, 4.0), row("rare", 0, 1, 50.0)],
                  ("sc-domain:b.example", "query"): [row("blog", 1, 30, 4.0)],
                  ("sc-domain:a.example", "device"): [row("MOBILE", 3, 50, 4.0)]},
            sitemaps={"sc-domain:a.example": [
                {"path": "https://blog.a.example/sitemap.xml", "lastDownloaded": "2026-11-07T01:00:00Z",
                 "errors": "0", "warnings": "2", "contents": [{"submitted": "12"}, {"submitted": "3"}]},
                {"path": "https://a.example/sitemap.xml", "errors": "0"}]})
        self.sc = SearchConsole(self.fake.request, "svc@proj.iam.gserviceaccount.com")

    def test_one_site_over_two_properties(self):
        r = self.sc.report(SITES["blog.a.example"], 7, now=NOW)
        self.assertTrue(r["connected"])
        self.assertEqual([p["site_url"] for p in r["properties"]], ["sc-domain:a.example", "sc-domain:b.example"])
        self.assertEqual(r["sources"], [{"property": "sc-domain:a.example", "hosts": ["blog.a.example"]},
                                        {"property": "sc-domain:b.example", "hosts": ["blog.b.example"]}])
        self.assertEqual(r["range"], {"start": "2026-11-02", "end": "2026-11-08"})
        self.assertEqual([d["day"] for d in r["series"]][0], "2026-11-02")
        self.assertEqual(len(r["series"]), 7)
        nov6 = next(d for d in r["series"] if d["day"] == "2026-11-06")
        self.assertEqual((nov6["clicks"], nov6["impressions"]), (2, 40))
        self.assertEqual(nov6["position"], 5.0)                      # (8*10 + 4*30) / 40
        self.assertEqual(r["latest"], "2026-11-06")
        # Oct 30 is before the range: it says how far behind Google is, nothing more.
        self.assertEqual(r["totals"], {"clicks": 4, "impressions": 80, "ctr": 0.05, "position": 4.0})
        self.assertEqual(r["queries"][0], {"key": "blog", "clicks": 4, "impressions": 80, "ctr": 0.05,
                                           "position": 4.0})
        self.assertEqual(r["devices"][0]["key"], "MOBILE")
        self.assertEqual(r["sitemaps"], [{"path": "https://blog.a.example/sitemap.xml",
                                          "read": "2026-11-07T01:00:00Z", "submitted_at": "", "pending": False,
                                          "index": False, "errors": 0, "warnings": 2, "urls": 15}])

    def test_what_it_asks_google(self):
        self.sc.report(SITES["blog.a.example"], 30, now=NOW)
        bodies = self.fake.bodies("sc-domain:a.example")
        self.assertEqual(set(bodies), {"date", "query", "page", "country", "device"})
        self.assertEqual(bodies["query"]["startDate"], "2026-10-10")
        self.assertEqual(bodies["query"]["endDate"], "2026-11-08")
        self.assertEqual(bodies["query"]["dataState"], "all")
        expr = bodies["page"]["dimensionFilterGroups"][0]["filters"][0]["expression"]
        self.assertIn("blog\\.a\\.example", expr)
        self.assertNotIn("blog\\.b\\.example", expr)
        self.assertEqual(self.fake.bodies("https://unverified.example/"), {})

    def test_a_short_range_still_finds_googles_newest_day(self):
        r = self.sc.report(SITES["blog.a.example"], 1, now=NOW)
        self.assertEqual(self.fake.bodies("sc-domain:a.example")["date"]["startDate"], "2026-11-02")
        self.assertEqual([d["day"] for d in r["series"]], ["2026-11-08"])
        self.assertEqual(r["totals"]["impressions"], 0)
        self.assertIsNone(r["totals"]["position"])
        self.assertEqual(r["latest"], "2026-11-06")

    def test_kept_a_while(self):
        self.sc.report(SITES["blog.a.example"], 7, now=NOW)
        n = len(self.fake.calls)
        self.sc.report(SITES["blog.a.example"], 7, now=NOW)
        self.assertEqual(len(self.fake.calls), n)
        later = time.time() + google.REPORT_TTL + google.PROPERTIES_TTL
        with mock.patch.object(google.time, "time", lambda: later):
            self.sc.report(SITES["blog.a.example"], 7, now=NOW)
        self.assertGreater(len(self.fake.calls), n)

    def test_no_property(self):
        r = self.sc.report(SITES["c.example"], 7, now=NOW)
        self.assertEqual((r["sources"], r["totals"], r["hosts"]), ([], None, ["c.example"]))
        r = self.sc.report(SITES["*.a.example"], 7, now=NOW)
        self.assertEqual((r["sources"], r["hosts"]), ([], []))

    def test_pacific_days(self):
        self.assertEqual(google.pacific_today(datetime(2026, 11, 9, 5, 0, tzinfo=timezone.utc)).isoformat(), "2026-11-08")
        self.assertEqual(google.pacific_today(datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc)).isoformat(), "2026-11-09")


class SigningIn(unittest.TestCase):
    def test_sigv4_matches_aws_example(self):
        # The IAM ListUsers example in AWS's Signature Version 4 documentation.
        headers = {"content-type": "application/x-www-form-urlencoded; charset=utf-8", "host": "iam.amazonaws.com",
                   "x-amz-date": "20150830T123600Z"}
        got = google.sigv4("GET", "iam.amazonaws.com", "Action=ListUsers&Version=2010-05-08", headers, "us-east-1",
                           "iam", "AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", "20150830T123600Z")
        self.assertEqual(got, "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/iam/aws4_request, "
                              "SignedHeaders=content-type;host;x-amz-date, "
                              "Signature=5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7")

    def test_subject_token(self):
        audience = "//iam.googleapis.com/projects/1/locations/global/workloadIdentityPools/p/providers/aws"
        token = google.aws_subject_token(audience, "us-east-1", "AKID", "secret", "session",
                                         now=datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc))
        req = json.loads(urllib.parse.unquote(token))
        self.assertEqual(req["url"], "https://sts.us-east-1.amazonaws.com?Action=GetCallerIdentity&Version=2011-06-15")
        self.assertEqual(req["method"], "POST")
        headers = {h["key"]: h["value"] for h in req["headers"]}
        self.assertEqual(headers["x-goog-cloud-target-resource"], audience)
        self.assertEqual(headers["x-amz-security-token"], "session")
        self.assertEqual(headers["x-amz-date"], "20261108T120000Z")
        self.assertIn("SignedHeaders=host;x-amz-date;x-amz-security-token;x-goog-cloud-target-resource,",
                      headers["Authorization"])
        self.assertIn("Credential=AKID/20261108/us-east-1/sts/aws4_request", headers["Authorization"])

    def test_from_aws_exchanges_at_googles_sts(self):
        seen = []

        def fetch(method, url, token="", body=None, form=None, **kw):
            seen.append((url, form))
            return {"access_token": "federated", "expires_in": 3600}

        env = {"AWS_REGION": "us-west-2", "AWS_ACCESS_KEY_ID": "AKID", "AWS_SECRET_ACCESS_KEY": "x",
               "AWS_SESSION_TOKEN": "t"}
        token, until = google.from_aws("//aud", env, fetch)()
        self.assertEqual(token, "federated")
        self.assertGreater(until, time.time() + 3000)
        url, form = seen[0]
        self.assertEqual(url, google.STS)
        self.assertEqual(form["subject_token_type"], "urn:ietf:params:aws:token-type:aws4_request")
        self.assertEqual(form["audience"], "//aud")
        self.assertIn("sts.us-west-2.amazonaws.com", urllib.parse.unquote(form["subject_token"]))

    def test_tokens_act_as_the_account_per_scope(self):
        sources, minted = [], []

        def source():
            sources.append(1)
            return "base", time.time() + 3600

        def fetch(method, url, token="", body=None, **kw):
            if url.endswith(":generateAccessToken"):
                minted.append((token, body["scope"]))
                self.assertIn("/serviceAccounts/svc%40proj.iam.gserviceaccount.com:", url)
                at = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
                return {"accessToken": f"as-svc-{len(minted)}", "expireTime": at}
            return {"token": token}

        t = Tokens("svc@proj.iam.gserviceaccount.com", source, fetch)
        self.assertEqual(t.request("GET", "https://x/", google.READ), {"token": "as-svc-1"})
        self.assertEqual(t.request("GET", "https://x/", google.READ), {"token": "as-svc-1"})
        self.assertEqual(t.request("GET", "https://x/", google.VERIFY), {"token": "as-svc-2"})
        self.assertEqual((len(sources), minted), (1, [("base", [google.READ]), ("base", [google.VERIFY])]))

    def test_from_env(self):
        self.assertIsNone(SearchConsole.from_env("", {}))
        self.assertIsNone(SearchConsole.from_env("svc@p", {"AWS_LAMBDA_FUNCTION_NAME": "carterlitics"}))
        self.assertIsNotNone(SearchConsole.from_env("svc@p", {"GOOGLE_AUDIENCE": "//aud"}))
        self.assertEqual(SearchConsole.from_env("svc@p", {}).account, "svc@p")    # gcloud, on a laptop

    def test_config(self):
        self.assertEqual(CFG.google_account, "carterlitics@proj.iam.gserviceaccount.com")
        self.assertEqual(Config.from_data({"google": {"project": "p", "service_account": "reader"}}).google_account,
                         "reader@p.iam.gserviceaccount.com")
        self.assertEqual(Config.from_data({"google": {"account": "x@y.iam.gserviceaccount.com"}}).google_account,
                         "x@y.iam.gserviceaccount.com")
        self.assertEqual(Config.from_data({}).google_account, "")
        with mock.patch.dict("os.environ", {"CARTERLITICS_CONFIG": json.dumps(CFG.to_data())}):
            self.assertEqual(Config.from_env().google_account, CFG.google_account)


class Console(unittest.TestCase):
    def app(self, search):
        cfg = Config.from_data(DATA, session_secret="s" * 32, dev=True)
        return App(cfg, Store(cfg, MemoryTable(), MemoryBlobs()), search)

    def get(self, app, path):
        url = urllib.parse.urlsplit(path)
        req = Request(method="GET", path=f"/_c/api{url.path}", query=dict(urllib.parse.parse_qsl(url.query)),
                      headers={"host": "a.example"}, cookies={}, body=b"")
        with mock.patch.object(app_module.auth, "session_user", lambda r, c: {"id": "admin"}):
            res = app.handle(req)
        body = gzip.decompress(res.body) if res.headers.get("Content-Encoding") == "gzip" else res.body
        return res.status, json.loads(body)

    def test_not_connected(self):
        status, d = self.get(self.app(None), "/search?site=a.example")
        self.assertEqual((status, d["connected"]), (200, False))
        self.assertIn("search-console", d["reason"])

    def test_report_and_what_each_property_covers(self):
        fake = FakeGoogle({"sc-domain:a.example": "siteOwner"},
                          rows={("sc-domain:a.example", "query"): [row("a", 1, 2, 3.0)]})
        status, d = self.get(self.app(SearchConsole(fake.request, "svc@p")), "/search?site=blog.a.example&days=7")
        self.assertEqual(status, 200)
        self.assertEqual(d["queries"][0]["key"], "a")
        self.assertEqual(d["covers"], {"sc-domain:a.example": ["a.example", "blog.a.example"]})

    def test_google_says_no(self):
        def refuse(*a, **k):
            raise GoogleError(403, "User does not have sufficient permission")
        status, d = self.get(self.app(SearchConsole(refuse, "svc@p")), "/search?site=a.example")
        self.assertEqual((status, d["connected"]), (200, False))
        self.assertIn("sufficient permission", d["reason"])

    def test_needs_a_session(self):
        app = self.app(None)
        res = app.handle(Request(method="GET", path="/_c/api/search", query={"site": "a.example"},
                                 headers={"host": "a.example"}, cookies={}, body=b""))
        self.assertEqual(res.status, 401)

    def test_lambda_task(self):
        fake = FakeGoogle({"sc-domain:a.example": "siteRestrictedUser"})
        with mock.patch.object(app_module, "_app", self.app(SearchConsole(fake.request, "svc@p"))):
            got = app_module.lambda_handler({"task": "search-console"}, None)
        self.assertEqual(got, {"ok": True, "account": "svc@p", "properties": [
            {"site_url": "sc-domain:a.example", "permission": "siteRestrictedUser"}]})
        with mock.patch.object(app_module, "_app", self.app(None)):
            got = app_module.lambda_handler({"task": "search-console"}, None)
        self.assertFalse(got["ok"])


class Demo(unittest.TestCase):
    def test_demo_reports(self):
        from carterlitics.demo import search_console

        cfg = Config.load(str(ROOT / "carterlitics.toml"), dev=True)
        sc = search_console(cfg)
        r = sc.report(cfg.sites["cartercole.net"], 30)
        self.assertGreater(r["totals"]["clicks"], 0)
        self.assertTrue(all(urllib.parse.urlsplit(p["key"]).hostname == "cartercole.net" for p in r["pages"]))
        self.assertEqual(len(sc.report(cfg.sites["resume.cartercole.net"], 7)["sources"]), 2)


if __name__ == "__main__":
    unittest.main()
