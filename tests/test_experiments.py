"""Experiments: their configuration, the statistics, the collector's counting, and c.js
showing each visitor their arm.

    python3 -m unittest discover tests
"""
from __future__ import annotations

import json
import shutil
import sys
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from carterlitics import app as app_module  # noqa: E402
from carterlitics import experiments  # noqa: E402
from carterlitics.app import App  # noqa: E402
from carterlitics.backends import MemoryBlobs, MemoryTable  # noqa: E402
from carterlitics.config import Config, Experiment  # noqa: E402
from carterlitics.store import Store, day_of, now  # noqa: E402
from test_collector import SAFARI, SECRET, SID, VID, Client, Page, body  # noqa: E402

HERO = {
    "key": "hero", "name": "Home headline", "status": "running", "pages": ["/start"], "goal": "event:sign_up",
    "metrics": ["/pricing", "event:buy"],
    "variant": [{"id": "a", "name": "Original"},
                {"id": "b", "name": "Faster", "css": ".hero { color: red }", "text": {".hero h1": "Faster"},
                 "attrs": {".hero a": {"href": "/go"}}}],
}
CTA = {"key": "cta", "status": "running", "goal": "/pricing", "allocation": "thompson", "explore": 0.2,
       "variant": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}
GRID = {"key": "grid", "status": "running", "pages": ["/"], "goal": "event:sign_up",
        "factor": [{"id": "head", "variant": [{"id": "a"}, {"id": "b"}]},
                   {"id": "button", "variant": [{"id": "blue"}, {"id": "green"}]}]}


def data(*exps, **site):
    return {"console": "a.example", "auth": {"issuer": "https://issuer.example"},
            "site": [{"id": "a.example", "url": "https://a.example", "experiment": list(exps), **site},
                     {"id": "b.example", "url": "https://b.example"}]}


def make(*exps) -> App:
    cfg = Config.from_data(data(*exps), session_secret=SECRET, origin_secret="origin", server_secret="server")
    return App(cfg, Store(cfg, MemoryTable(), MemoryBlobs()))


def parse(**changes) -> Experiment:
    return Experiment.parse({**HERO, **changes}, "a.example")


class ExperimentConfig(unittest.TestCase):
    def test_an_ab_test(self):
        exp = parse()
        self.assertEqual([a.id for a in exp.arms], ["a", "b"])
        self.assertEqual([a.name for a in exp.arms], ["Original", "Faster"])
        self.assertEqual([m.text for m in exp.all_metrics], ["event:sign_up", "/pricing", "event:buy"])
        self.assertTrue(exp.changes)
        self.assertEqual(Experiment.parse(exp.to_data(), "a.example"), exp)

    def test_a_multivariate_test_is_every_combination(self):
        exp = Experiment.parse(GRID, "a.example")
        self.assertTrue(exp.multivariate)
        self.assertEqual([a.id for a in exp.arms], ["a.blue", "a.green", "b.blue", "b.green"])
        self.assertEqual(exp.arms[3].name, "head b · button green")
        self.assertEqual(Experiment.parse(exp.to_data(), "a.example"), exp)

    def test_what_isnt_allowed(self):
        bad = [
            ({"key": "Hero"}, "key"),
            ({"status": "live"}, "status"),
            ({"goal": ""}, "goal"),
            ({"goal": "sign_up"}, "goal"),
            ({"pages": ["event:x"]}, "pages are paths"),
            ({"status": "done"}, "names its winner"),
            ({"winner": "z"}, "isn't one of its variants"),
            ({"traffic": 0}, "traffic"),
            ({"allocation": "random"}, "allocation"),
            ({"variant": [{"id": "a"}]}, "2 to 8 variants"),
            ({"variant": [{"id": "a"}, {"id": "a"}]}, "share an id"),
            ({"variant": [{"id": "a"}, {"id": "b", "attrs": {"a": {"onclick": "x()"}}}]}, "isn't an attribute"),
            ({"variant": [{"id": "a"}, {"id": "b", "attrs": {"a": {"href": " JavaScript:alert(1)"}}}]}, "script URL"),
            ({"variant": [{"id": "a"}, {"id": "b", "attrs": {"a": {"href": "java\tscript:alert(1)"}}}]}, "script URL"),
            ({"variant": [{"id": "a"}, {"id": "b", "attrs": {"a": {"href": "\x01javascript:x"}}}]}, "script URL"),
            ({"variant": [{"id": "a"}, {"id": "b", "redirect": "https://elsewhere.example/"}]}, "a path on the site"),
            ({"variant": [{"id": "a"}, {"id": "b", "redirect": "/\\elsewhere.example/"}]}, "a path on the site"),
            ({"variant": [{"id": "a"}, {"id": "b", "redirect": "/\t/elsewhere.example/"}]}, "a path on the site"),
            ({"variant": [{"id": "a", "redirect": "/x"}, {"id": "b"}]}, "the original"),
            ({"pages": [], "variant": [{"id": "a"}, {"id": "b", "redirect": "/b"}]}, "needs pages"),
            ({"pages": ["/start*"], "variant": [{"id": "a"}, {"id": "b", "redirect": "/start-b"}]}, "own pages"),
            ({"factor": GRID["factor"]}, "not both"),
        ]
        for change, message in bad:
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, message):
                parse(**change)
        too_many = {**GRID, "factor": [{"id": f"f{i}", "variant": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}
                                       for i in range(3)]}
        with self.assertRaisesRegex(ValueError, "27 combinations"):
            Experiment.parse(too_many, "a.example")

    def test_on_a_site(self):
        with self.assertRaisesRegex(ValueError, "two experiments share a key"):
            Config.from_data(data(HERO, HERO))
        with self.assertRaisesRegex(ValueError, "don't load it"):
            Config.from_data(data(HERO, client=False))

    def test_kept_out_of_the_environment_and_in_the_package(self):
        cfg = Config.from_data(data(HERO, CTA))
        self.assertNotIn("experiment", json.dumps(cfg.to_data()))
        bare = Config.from_data(cfg.to_data())
        self.assertEqual(bare.sites["a.example"].experiments, ())
        packaged = json.loads(json.dumps(cfg.experiments_data()))
        self.assertEqual(bare.with_experiments({**packaged, "gone.example": []}).sites["a.example"].experiments,
                         cfg.sites["a.example"].experiments)


class Statistics(unittest.TestCase):
    def test_wilson(self):
        low, high = experiments.wilson(0, 10)
        self.assertEqual(low, 0)
        self.assertAlmostEqual(high, 0.2775, places=3)
        low, high = experiments.wilson(5, 10)
        self.assertAlmostEqual(low, 0.2366, places=3)
        self.assertAlmostEqual(high, 0.7634, places=3)
        self.assertEqual(experiments.wilson(0, 0), (None, None))

    def test_chance_to_beat(self):
        self.assertAlmostEqual(experiments.beat(30, 1000, 30, 1000), 0.5, places=2)
        self.assertGreater(experiments.beat(60, 1000, 30, 1000), 0.99)
        self.assertAlmostEqual(experiments.beat(40, 900, 30, 1000) + experiments.beat(30, 1000, 40, 900), 1, places=6)
        self.assertIsNone(experiments.beat(1, 0, 1, 10))

    def test_chance_to_be_best(self):
        chances = experiments.best([(30, 1000), (45, 1000), (20, 1000)], seed="x")
        self.assertAlmostEqual(sum(chances), 1)
        self.assertEqual(chances.index(max(chances)), 1)
        self.assertEqual(chances, experiments.best([(30, 1000), (45, 1000), (20, 1000)], seed="x"))

    def test_sequential(self):
        even = experiments.sequential([(30 * k, 1000 * k, 30 * k, 1000 * k) for k in range(1, 8)])
        self.assertEqual(even["p"], 1)
        self.assertLess(even["low"], 0)
        self.assertGreater(even["high"], 0)
        looks = [(20 * k, 1000 * k, 32 * k, 1000 * k) for k in range(1, 15)]
        ps = [experiments.sequential(looks[:k])["p"] for k in range(1, len(looks) + 1)]
        self.assertEqual(ps, sorted(ps, reverse=True))           # looking again never makes it less sure
        self.assertLess(ps[-1], 0.05)
        self.assertGreater(ps[0], 0.05)
        last = experiments.sequential(looks)
        self.assertLess(last["low"], 0.012)
        self.assertGreater(last["high"], 0.012)
        tiny = experiments.sequential([(0, 5, 3, 5)])               # too few to count
        self.assertEqual(tiny["p"], 1)

    def test_split_weights(self):
        exp = parse(variant=[{"id": "a", "weight": 3}, {"id": "b"}])
        self.assertEqual(experiments.weights(exp, {}), {"a": 0.75, "b": 0.25})
        grid = Experiment.parse(GRID, "a.example")
        self.assertEqual(set(experiments.weights(grid, {}).values()), {0.25})

    def test_bandits_move_traffic_to_what_works(self):
        exp = Experiment.parse(CTA, "a.example")
        start = experiments.weights(exp, {})
        self.assertTrue(all(abs(v - 1 / 3) < 0.05 for v in start.values()))
        goal = exp.goal.text
        seen = {"a": Counter({"e": 900, ("c", goal): 27}), "b": Counter({"e": 900, ("c", goal): 63}),
                "c": Counter({"e": 900, ("c", goal): 20})}
        thompson = experiments.weights(exp, seen)
        self.assertAlmostEqual(sum(thompson.values()), 1)
        self.assertGreater(thompson["b"], 0.85)              # all it may have: 0.8 plus its explore share
        self.assertGreaterEqual(min(thompson.values()), 0.2 / 3 - 1e-9)   # explore, split evenly
        greedy = experiments.weights(Experiment.parse({**CTA, "allocation": "greedy"}, "a.example"), seen)
        self.assertAlmostEqual(greedy["b"], 0.2 / 3 + 0.8)
        self.assertAlmostEqual(greedy["a"], 0.2 / 3)

    def test_buckets(self):
        one = experiments.bucket("s", "a.example", "hero", "198.51.100.7", SAFARI)
        self.assertTrue(0 <= one < 1)
        self.assertEqual(one, experiments.bucket("s", "a.example", "hero", "198.51.100.200", SAFARI))   # its /24
        self.assertNotEqual(one, experiments.bucket("s", "a.example", "cta", "198.51.100.7", SAFARI))
        self.assertNotEqual(one, experiments.bucket("s", "a.example", "hero", "198.51.100.7", "curl/8"))
        self.assertNotEqual(one, experiments.bucket("t", "a.example", "hero", "198.51.100.7", SAFARI))


class Collector(unittest.TestCase):
    def setUp(self):
        self.app = make(HERO, CTA, GRID, {**HERO, "key": "later", "status": "paused"},
                        {**HERO, "key": "draft", "status": "draft"},
                        {**HERO, "key": "old", "status": "done", "winner": "b"})
        self.call = Client(self.app)

    def client_js(self, site="a.example") -> str:
        res = self.call("GET", "/_c/c.js", site=site)
        self.assertEqual(res.status, 200)
        return res.body.decode()

    def served(self) -> dict:
        source = self.client_js()
        start = source.index("var EXPERIMENTS = ") + len("var EXPERIMENTS = ")
        found, _ = json.JSONDecoder().raw_decode(source[start:])
        return {x["k"]: x for x in found}

    def test_c_js_carries_the_sites_experiments(self):
        x = self.served()
        self.assertEqual(sorted(x), ["cta", "draft", "grid", "hero", "old"])       # paused: not there
        self.assertEqual((x["draft"]["s"], x["old"]["s"], x["old"]["win"]), ("draft", "done", "b"))
        hero = x["hero"]
        self.assertEqual(hero["arms"], [{"id": "a", "w": 0.5}, {"id": "b", "w": 0.5}])
        self.assertEqual(hero["pages"], ["/start"])
        self.assertEqual(hero["m"][0], {"t": "event:sign_up", "k": "event", "n": "sign_up"})
        self.assertEqual(hero["f"][0]["lv"][1]["text"], [[".hero h1", "Faster"]])
        self.assertEqual([f["id"] for f in x["grid"]["f"]], ["head", "button"])
        self.assertIn("/*__EXPERIMENTS__*/null", self.client_js("b.example"))

    def test_a_bandits_shares_follow_its_counters(self):
        before = self.served()["cta"]["arms"]
        self.assertTrue(all(abs(a["w"] - 1 / 3) < 0.05 for a in before))
        for arm, converted in (("a", 3), ("b", 40), ("c", 2)):
            items = [{"t": "x", "e": "cta", "v": arm}] * 200 + \
                [{"t": "xc", "e": "cta", "v": arm, "g": "/pricing", "u": 1}] * converted
            for i in range(0, len(items), 50):
                self.call("POST", "/_c/i", {"items": items[i:i + 50]})
        self.assertEqual(self.served()["cta"]["arms"], before)                    # reused for a minute
        with mock.patch.object(app_module.time, "monotonic", lambda: 1e12):
            after = {a["id"]: a["w"] for a in self.served()["cta"]["arms"]}
        self.assertGreater(after["b"], 0.8)

    def test_buckets_for_visitors_without_consent(self):
        res = self.call("GET", "/_c/x", ip="198.51.100.7")
        got = body(res)["u"]
        self.assertEqual(sorted(got), ["cta", "grid", "hero"])                     # running, for everyone
        self.assertEqual(got, body(self.call("GET", "/_c/x", ip="198.51.100.99"))["u"])
        self.assertEqual(res.headers["Cache-Control"], "no-store")
        self.assertEqual(body(self.call("GET", "/_c/x", headers={"dnt": "1"}))["u"], {})
        self.assertEqual(body(self.call("GET", "/_c/x", ua="Googlebot/2.1"))["u"], {})
        self.assertEqual(self.call("GET", "/_c/x", site="nowhere.example").status, 404)

    def counters(self, key="hero") -> dict:
        items = self.app.store.experiment(self.app.cfg.sites["a.example"], key)
        return {i["sk"].split("#", 1)[1]: i["n"] for i in items}

    def test_exposures_and_conversions(self):
        self.call("POST", "/_c/i", {"items": [
            {"t": "x", "e": "hero", "v": "b"},
            {"t": "xc", "e": "hero", "v": "b", "g": "event:sign_up", "u": 1},
            {"t": "xc", "e": "hero", "v": "b", "g": "event:sign_up"},
            {"t": "xc", "e": "hero", "v": "b", "g": "event:buy", "u": 1, "val": 12.5},
            {"t": "x", "e": "hero", "v": "z"},                                        # not an arm
            {"t": "xc", "e": "hero", "v": "b", "g": "event:other", "u": 1},           # not a metric
            {"t": "x", "e": "later", "v": "b"},                                       # paused
            {"t": "x", "e": "draft", "v": "b"},
            {"t": "x", "e": "nope", "v": "a"},
            {"t": "x", "e": "hero", "v": "a"},
        ]})
        self.assertEqual(self.counters(), {"e#b": 1, "e#a": 1, "n#b#event:sign_up": 2, "c#b#event:sign_up": 1,
                                           "n#b#event:buy": 1, "c#b#event:buy": 1, "v#b#event:buy": 1250})
        self.assertEqual(self.counters("later"), {})
        self.call("POST", "/_c/i", {"items": [{"t": "x", "e": "hero", "v": "a"}]}, headers={"sec-gpc": "1"})
        self.call("POST", "/_c/i", {"items": [{"t": "x", "e": "hero", "v": "a"}]}, ua="Googlebot/2.1")
        self.call("POST", "/_c/i", {"wd": True, "items": [{"t": "x", "e": "hero", "v": "a"}]})
        self.assertEqual(self.counters()["e#a"], 1)

    def test_a_consented_session_keeps_its_arms(self):
        ids = {"sid": SID, "vid": VID, "sd": day_of(now())}
        self.call("POST", "/_c/i", {**ids, "items": [{"t": "pv", "p": "/start", "new": True},
                                                     {"t": "x", "e": "hero", "v": "b"}]})
        self.call("POST", "/_c/i", {**ids, "items": [{"t": "x", "e": "grid", "v": "a.green"}]})
        self.call.sign_in()
        sessions = body(self.call("GET", "/_c/api/sessions", query={"site": "a.example"}))["items"]
        self.assertEqual((sessions[0]["x:hero"], sessions[0]["x:grid"]), ("b", "a.green"))

    def test_the_report(self):
        def send(arm, n, converted):
            for i in range(n):
                items = [{"t": "x", "e": "hero", "v": arm}]
                if i < converted:
                    items.append({"t": "xc", "e": "hero", "v": arm, "g": "event:sign_up", "u": 1})
                self.call("POST", "/_c/i", {"items": items})
        send("a", 400, 12)
        send("b", 400, 40)
        self.assertEqual(self.call("GET", "/_c/api/experiments", query={"site": "a.example"}).status, 401)
        self.call.sign_in()
        res = self.call("GET", "/_c/api/experiments", query={"site": "a.example"})
        self.assertEqual(res.status, 200)
        reports = {r["key"]: r for r in body(res)["experiments"]}
        hero = reports["hero"]
        self.assertEqual((hero["units"], hero["days"], len(hero["series"])), (800, 1, 1))
        a, b = hero["arms"]
        self.assertEqual((a["units"], a["metrics"]["event:sign_up"]["converted"]), (400, 12))
        goal = b["metrics"]["event:sign_up"]
        self.assertEqual((goal["rate"], goal["lift"]), (0.1, 2.333333))
        self.assertLess(goal["p"], 0.05)
        self.assertEqual(goal["significant"], "better")
        self.assertGreater(goal["beat"], 0.99)
        self.assertLess(goal["lift_low"], goal["lift"])
        self.assertEqual(hero["verdict"]["state"], "winner")
        self.assertEqual(hero["verdict"]["arm"], "b")
        self.assertEqual(reports["old"]["verdict"]["state"], "done")
        self.assertEqual([e["factor"] for e in reports["grid"]["effects"]], ["head", "button"])
        self.assertEqual(reports["later"]["verdict"]["state"], "none")


def served(*exps) -> list[dict]:
    """The experiments as c.js gets them, from their configuration."""
    out = []
    for raw in exps:
        exp = Experiment.parse(raw, "a.example")
        out.append(experiments.client(exp, experiments.weights(exp, {})))
    return out


@unittest.skipUnless(shutil.which("node"), "needs node to run c.js")
class ClientExperiments(Page, unittest.TestCase):
    """c.js showing each visitor their arm, the same one every time, and counting it once."""

    PAGE = ("var h1 = node('H1', { text: 'Old' }, ['.hero h1']);"
            "var link = node('A', { href: '/old' }, ['.hero a']);")

    def visit(self, after="", exps=(HERO,), url="https://a.example/start", before="", wait=30, **spec):
        """The page's elements are there before c.js runs; what the visitor does comes a moment
        after, once the collector has answered."""
        return self.page(before=self.PAGE + before, after=f"setTimeout(function () {{ {after} }}, 5);",
                         experiments=served(*exps), url=url, wait=wait, **spec)

    @staticmethod
    def sent(out, kind):
        return [{k: v for k, v in i.items() if k != "p"} for i in out["items"] if i["t"] == kind]

    def test_without_consent_the_collector_places_them(self):
        out = self.visit("cl('track', 'sign_up'); cl('track', 'sign_up'); cl('track', 'buy', { value: 9.5 });",
                         answers=[{"u": {"hero": 0.7}}], classes=["cl-wait"])
        self.assertEqual(out["gets"], ["/_c/x"])
        self.assertEqual(self.sent(out, "x"), [{"t": "x", "e": "hero", "v": "b"}])
        self.assertEqual(self.sent(out, "xc"), [
            {"t": "xc", "e": "hero", "v": "b", "g": "event:sign_up", "u": 1},
            {"t": "xc", "e": "hero", "v": "b", "g": "event:sign_up"},
            {"t": "xc", "e": "hero", "v": "b", "g": "event:buy", "u": 1, "val": 9.5}])
        self.assertEqual(out["styles"], [["hero", ".hero { color: red }"]])
        self.assertEqual(out["nodes"][0]["text"], "Faster")
        self.assertEqual(out["nodes"][1]["attrs"]["href"], "/go")
        self.assertEqual(out["classes"], [])                                           # the anti-flicker class
        self.assertEqual(json.loads(out["session"]["cl_x"])["hero"]["v"], "b")
        self.assertNotIn("cl_x", out["local"])
        # The next page in the tab: the same arm, nothing asked, nothing counted again but the new metric.
        again = self.visit("cl('track', 'sign_up');", url="https://a.example/pricing", session=out["session"])
        self.assertEqual(again["gets"], [])
        self.assertEqual(self.sent(again, "x"), [])
        self.assertEqual(self.sent(again, "xc"), [{"t": "xc", "e": "hero", "v": "b", "g": "/pricing", "u": 1},
                                                  {"t": "xc", "e": "hero", "v": "b", "g": "event:sign_up"}])

    def test_the_original_is_the_page_as_written(self):
        out = self.visit(answers=[{"u": {"hero": 0.2}}])
        self.assertEqual(self.sent(out, "x"), [{"t": "x", "e": "hero", "v": "a"}])
        self.assertEqual(out["styles"], [])
        self.assertEqual(out["nodes"][0]["text"], "Old")

    def test_consented_visitors_need_no_collector(self):
        cookies = {"consent": "all", "vid": VID}
        first = self.visit(cookies=cookies)
        self.assertEqual(first["gets"], [])
        arm = json.loads(first["local"]["cl_x"])["hero"]["v"]
        self.assertIn(arm, ("a", "b"))
        # Another tab, storage cleared: the same arm, from the cookie.
        other = self.visit(cookies=cookies)
        self.assertEqual(json.loads(other["local"]["cl_x"])["hero"]["v"], arm)
        # The visitor's own record wins over a tab's.
        kept = self.visit(cookies=cookies, local={"cl_x": json.dumps({"hero": {"v": "a", "s": 1}})},
                          session={"cl_x": json.dumps({"hero": {"v": "b"}})})
        self.assertEqual(self.sent(kept, "x"), [])
        self.assertEqual(kept["nodes"][0]["text"], "Old")

    def test_accepting_makes_the_tabs_arms_the_visitors(self):
        out = self.visit("cl('consent', 'all');", answers=[{"u": {"hero": 0.9}}])
        self.assertEqual(json.loads(out["local"]["cl_x"])["hero"], {"v": "b", "s": 1})
        out = self.visit("cl('consent', 'essential');", cookies={"consent": "all", "vid": VID},
                         local={"cl_x": json.dumps({"hero": {"v": "b", "s": 1}})}, dataset={"record": "off"})
        self.assertNotIn("cl_x", out["local"])

    def test_markup_carries_variants(self):
        markup = ("var a = node('SECTION', { 'data-cl-x': 'grid.head:a' }); var b = node('SECTION', "
                  "{ 'data-cl-x': 'grid.head:b', hidden: true }); var green = node('BUTTON', "
                  "{ 'data-cl-x': 'grid.button:green', hidden: true });")
        out = self.visit(before=markup, exps=(HERO, GRID), url="https://a.example/about",
                         answers=[{"u": {"grid": 0.6}}])
        self.assertEqual(self.sent(out, "x"), [{"t": "x", "e": "grid", "v": "b.blue"}])
        self.assertEqual([n["hidden"] for n in out["nodes"][2:]], [True, False, True])
        self.assertEqual(out["nodes"][0]["text"], "Old")                               # hero isn't on /about

    def test_the_page_can_ask(self):
        exps = ({**HERO, "pages": [], "variant": [{"id": "a"}, {"id": "b"}]},)
        before = "window.cl = function () { (cl.q = cl.q || []).push(arguments) };" \
                 "cl('variant', 'hero', function (v) { log.push(['got', v]) });"
        out = self.page(before=before, experiments=served(*exps), answers=[{"u": {"hero": 0.55}}], wait=20)
        self.assertIn(["got", "b"], out["log"])
        self.assertEqual(self.sent(out, "x"), [{"t": "x", "e": "hero", "v": "b"}])
        out = self.page(after="cl('variant', 'nope').then(function (v) { log.push(['got', v]) });",
                        experiments=served(*exps), wait=20)
        self.assertIn(["got", None], out["log"])

    def test_traffic_left_out(self):
        out = self.visit(exps=({**HERO, "traffic": 0.5},), answers=[{"u": {"hero": 0.7}}])
        self.assertEqual(self.sent(out, "x"), [])
        self.assertEqual(json.loads(out["session"]["cl_x"])["hero"], {"v": ""})
        self.assertEqual(out["nodes"][0]["text"], "Old")

    def test_nobody_who_said_no_and_no_automation(self):
        for spec in ({"dnt": True}, {"before": "navigator.webdriver = true;"}):
            with self.subTest(spec=spec):
                out = self.visit(answers=[{"u": {"hero": 0.9}}], **spec)
                self.assertEqual((out["gets"], self.sent(out, "x"), out["nodes"][0]["text"]), ([], [], "Old"))
                self.assertNotIn("cl_x", out["session"])

    def test_a_preview_is_never_counted(self):
        exps = (HERO, {**HERO, "key": "draft", "status": "draft"})
        out = self.visit("cl('track', 'sign_up');", exps=exps, url="https://a.example/start?cl_x=draft:b,hero:b")
        self.assertEqual((out["gets"], self.sent(out, "x"), self.sent(out, "xc")), ([], [], []))
        self.assertEqual(out["nodes"][0]["text"], "Faster")
        self.assertEqual(json.loads(out["session"]["cl_xf"]), {"draft": "b", "hero": "b"})
        ended = self.visit(url="https://a.example/start?cl_x=", session=out["session"], answers=[{"u": {"hero": 0.1}}])
        self.assertNotIn("cl_xf", ended["session"])
        self.assertEqual(ended["nodes"][0]["text"], "Old")

    def test_done_shows_the_winner_to_everyone(self):
        out = self.visit(exps=({**HERO, "status": "done", "winner": "b"},), dnt=True)
        self.assertEqual((out["gets"], self.sent(out, "x"), out["nodes"][0]["text"]), ([], [], "Faster"))

    def test_a_split_url_test_redirects(self):
        split = {**HERO, "variant": [{"id": "a"}, {"id": "b", "redirect": "/start-b"}]}
        out = self.page(experiments=served(split), url="https://a.example/start?utm_source=hn",
                        referrer="https://news.ycombinator.com/", answers=[{"u": {"hero": 0.8}}], wait=20)
        self.assertIn(["replace", "https://a.example/start-b?utm_source=hn"], out["log"])
        self.assertEqual([i["t"] for i in out["items"]], ["x"])                       # no page view for /start
        landed = self.page(experiments=served(split), url="https://a.example/start-b?utm_source=hn",
                           referrer="https://a.example/start?utm_source=hn", session=out["session"])
        pv, = [i for i in landed["items"] if i["t"] == "pv"]
        self.assertEqual((pv["p"], pv["r"]), ("/start-b", "https://news.ycombinator.com/"))
        self.assertEqual(self.sent(landed, "x"), [])

    def test_asked_after_giving_up_it_still_answers(self):
        """cl("variant") asked between the wait running out and a late answer: the original,
        as the page view is, rather than a promise that never settles."""
        out = self.visit(answers=[{"u": {"hero": 0.9}}], delay=1700, wait=1900, probe="window.__v", after=(
            "setTimeout(function () { cl('variant', 'hero').then(function (v) { window.__v = v === null ? 'original' : v; }); }, 1600);"))
        self.assertEqual(out["probe"], "original")

    def test_a_redirect_never_leaves_the_site(self):
        """Whatever the config let through, c.js sends a visitor only to a path on its site."""
        split = served({**HERO, "variant": [{"id": "a"}, {"id": "b", "redirect": "/start-b"}]})
        split[0]["f"][0]["lv"][1]["go"] = "/\\evil.example/x"    # as a browser reads it: //evil.example/x
        out = self.page(experiments=split, url="https://a.example/start", answers=[{"u": {"hero": 0.8}}], wait=20)
        self.assertFalse([e for e in out["log"] if isinstance(e, list) and e[0] == "replace"])

    def test_a_slow_collector_leaves_the_original(self):
        out = self.visit(answers=[{"u": {"hero": 0.9}}], delay=1700, wait=1900, classes=["cl-wait"])
        self.assertEqual(self.sent(out, "x"), [])
        self.assertEqual(out["nodes"][0]["text"], "Old")
        self.assertEqual(out["classes"], [])
        self.assertEqual(json.loads(out["session"]["cl_x"])["hero"], {"v": "b"})      # for the next page
        nxt = self.visit(session=out["session"])
        self.assertEqual((nxt["gets"], self.sent(nxt, "x")), ([], [{"t": "x", "e": "hero", "v": "b"}]))


if __name__ == "__main__":
    unittest.main()
