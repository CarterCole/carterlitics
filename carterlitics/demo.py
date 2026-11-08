"""Thirty days of made-up traffic, for `python3 -m carterlitics.dev --demo`: enough to see
every part of the console without deploying anything. Deterministic, so screenshots
compare. Everything goes through the real Store methods, with the clock moved back."""
from __future__ import annotations

import dataclasses
import random
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from unittest import mock

from . import store as store_module
from .config import Funnel
from .store import LogLine, Meta

PEOPLE = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (X11; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0",
    "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
]
BOTS = [
    ("Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)", "66.249.66.{}"),
    ("Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; GPTBot/1.2; +https://openai.com/gptbot)", "20.171.207.{}"),
    ("Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; ClaudeBot/1.0; +claudebot@anthropic.com)", "34.162.46.{}"),
    ("Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)", "40.77.167.{}"),
    ("Mozilla/5.0 (compatible; AhrefsBot/7.0; +http://ahrefs.com/robot/)", "5.39.1.{}"),
    ("facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)", "69.171.249.{}"),
    ("Feedly/1.0 (+http://www.feedly.com/fetcher.html; 14 subscribers)", "52.4.1.{}"),
    ("curl/8.7.1", "198.51.100.{}"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36", "45.148.10.{}"),
]
PROBES = ["/wp-login.php", "/.env", "/xmlrpc.php", "/.git/config", "/wp-admin/setup-config.php", "/phpmyadmin/"]
REFERRERS = [None, None, None, "https://news.ycombinator.com/item?id=4242", "https://www.google.com/",
             "https://duckduckgo.com/", "https://github.com/CarterCole", "https://www.reddit.com/r/rust/",
             "https://lobste.rs/s/abc"]
COUNTRIES = ["US", "US", "US", "GB", "DE", "CA", "FR", "NL", "IN", "JP", "AU", "BR"]
NETWORKS = [("7922", "Comcast Cable Communications, LLC"), ("701", "Verizon Business"),
            ("2856", "British Telecommunications PLC"), ("3320", "Deutsche Telekom AG"),
            ("16509", "Amazon.com, Inc.")]
# For sites with no funnels of their own. Some consented visitors walk the first one,
# dropping off along the way; `email_confirmed` comes from the site's backend.
FUNNELS = {
    "cartercole.com": [{"name": "Reader to sign-up",
                        "steps": ["/", "/blog", "/blog/*", "event:sign_up", "event:email_confirmed"]}],
    "cartercole.net": [{"name": "Project to source", "steps": ["/", "/rollopolis", "event:outbound?target=github.com"]}],
}
GO_ON = (0.74, 0.55, 0.42, 0.8, 0.8)   # the share of a step's visitors who take the next one
ERRORS = [
    ("error", "TypeError", "Cannot read properties of undefined (reading 'width')",
     "TypeError: Cannot read properties of undefined (reading 'width')\n    at layout (https://{host}/app-3f9a1c2e.js:112:18)\n    at render (https://{host}/app-3f9a1c2e.js:80:5)"),
    ("rejection", "Error", "Failed to fetch",
     "Error: Failed to fetch\n    at load (https://{host}/modal-77ab12cd.js:41:11)"),
    ("resource", "", "Failed to load <img> https://{host}/img/rollopolis/title-1440.webp", ""),
]


def configure(cfg):
    """The config with the demo's funnels on the sites that have none."""
    sites = {k: dataclasses.replace(s, funnels=tuple(Funnel.parse(f, k) for f in FUNNELS[k]))
             if k in FUNNELS and not s.funnels else s for k, s in cfg.sites.items()}
    return dataclasses.replace(cfg, sites=sites)


def fill(store, cfg, days: int = 30, seed: int = 7) -> None:
    rng = random.Random(seed)
    now = datetime.now(timezone.utc)
    today = now.replace(minute=0, second=0, microsecond=0)

    def past(at: datetime) -> datetime:
        return at if at < now else now - timedelta(minutes=rng.randrange(1, 90))
    for site in cfg.sites.values():
        paths = {"cartercole.net": ["/", "/", "/", "/mosaic", "/rollopolis", "/raindrive", "/nukumori", "/cartier",
                                    "/privacy"],
                 "cartercole.com": ["/", "/", "/blog", "/blog/hello-world", "/projects", "/blog/relay-ssr",
                                    "/privacy"]}.get(site.id, ["/", "/about"])
        for back in range(days - 1, -1, -1):
            base = today - timedelta(days=back)
            weekday = base.weekday() < 5
            visitors = int(rng.gauss(26 if weekday else 15, 6) * (2.4 if back == 9 else 1))  # one good day
            lines: list[LogLine] = []
            for v in range(max(3, visitors)):
                at = past(base.replace(hour=rng.randrange(0, 24 if back else max(1, base.hour + 1)),
                                       minute=rng.randrange(60), second=rng.randrange(60)))
                with mock.patch.object(store_module, "now", lambda at=at: at):
                    ua = rng.choice(PEOPLE)
                    ip = f"{rng.randrange(24, 220)}.{rng.randrange(256)}.{rng.randrange(256)}.{rng.randrange(1, 255)}"
                    asn, network = rng.choice(NETWORKS)
                    meta = Meta(ip=ip, user_agent=ua, country=rng.choice(COUNTRIES), asn=asn)
                    consent = rng.random() < 0.3
                    sid, vid = str(uuid.UUID(int=rng.getrandbits(128))), str(uuid.UUID(int=rng.getrandbits(128)))
                    ids = {"sid": sid, "vid": vid, "sd": at.strftime("%Y-%m-%d"),
                           "fp": f"{rng.getrandbits(128):032x}"} if consent else {}
                    ref = rng.choice(REFERRERS)
                    width = rng.choice([390, 414, 820, 1280, 1440, 1728])
                    walks = consent and bool(site.funnels) and rng.random() < 0.55
                    if walks:
                        for when, path in _journey(store, site, rng, at, now, ids, meta, width, ref):
                            lines.append(LogLine(when, site.host, ip, "GET", path, "", 200, ref or "", ua,
                                                 edge="Hit", seconds=0.004))
                    for n in range(0 if walks else rng.choice([1, 1, 1, 2, 3, 5])):
                        path = rng.choice(paths)
                        items = [{"t": "pv", "p": path, "r": ref if n == 0 else f"{site.url}/",
                                  **({"new": True} if n == 0 and consent else {})},
                                 {"t": "leave", "p": path, "ms": int(rng.expovariate(1 / 45000)) + 1500}]
                        if rng.random() < 0.08:
                            items.append({"t": "ev", "n": rng.choice(["outbound", "download", "copy-code", "zoom"]),
                                          "props": {"target": rng.choice(["github.com", "x.zip", "lightbox"])}})
                        if rng.random() < 0.03:
                            kind, name, message, stack = rng.choice(ERRORS)
                            items.append({"t": "err", "k": kind, "name": name,
                                          "m": message.format(host=site.host), "st": stack.format(host=site.host)})
                        store.ingest(site, {"w": width, "items": items, **ids}, meta)
                        lines.append(LogLine(at, site.host, ip, "GET", path, "", 200, ref or "", ua,
                                             edge=rng.choice(["Hit", "Hit", "Miss", "RefreshHit"]), seconds=0.004))
                    if consent:
                        store.chunk(site, {"sid": sid, "vid": vid, "sd": ids["sd"], "seq": 0, "p": "/",
                                           "events": _recording(at, site)}, meta)
            for _ in range(int(visitors * rng.uniform(1.5, 3.2))):
                ua, net = rng.choice(BOTS)
                at = past(base.replace(hour=rng.randrange(0, 24 if back else max(1, base.hour + 1)),
                                       minute=rng.randrange(60)))
                probe = "Chrome/120" in ua or ua.startswith("curl")
                path = rng.choice(PROBES) if probe and rng.random() < 0.7 else rng.choice(paths + ["/robots.txt", "/sitemap.xml", "/rss.xml"])
                lines.append(LogLine(at, site.host, net.format(rng.randrange(1, 250)), "GET", path, "",
                                     404 if path in PROBES else 200, "", ua, edge="Miss" if path in PROBES else "Hit"))
            store.ingest_log(lines)


def _journey(store, site, rng, at, now, ids, meta, width, ref) -> list[tuple[datetime, str]]:
    """One consented visit along the site's first funnel, as far as it gets. Returns
    the pages it requested, for the CloudFront log."""
    steps = [s.text for s in site.funnels[0].steps]
    taken = steps[:1]
    for step, go in zip(steps[1:], GO_ON):
        if rng.random() >= go:
            break
        taken.append(step)
    gaps = [rng.randrange(8, 240) for _ in taken]
    start = min(at, now - timedelta(seconds=sum(gaps) + 60))
    uid, path, pages = f"user-{rng.getrandbits(32):08x}", "/", []
    for k, step in enumerate(taken):
        when = start + timedelta(seconds=sum(gaps[:k + 1]))
        with mock.patch.object(store_module, "now", lambda when=when: when):
            if step.startswith("event:"):
                name, _, query = step[len("event:"):].partition("?")
                props = dict(urllib.parse.parse_qsl(query)) or {"method": rng.choice(["email", "google"])}
                if name == "email_confirmed":
                    store.server_events(site, [{"name": name, "uid": uid, "ip": meta.ip, "ua": meta.user_agent,
                                                "path": "/auth/confirm"}], meta)
                else:
                    # Signing up identifies the visitor, so the backend's event can join the session.
                    who = {"uid": uid} if name == "sign_up" else {}
                    store.ingest(site, {"w": width, **ids, **who,
                                        "items": [{"t": "ev", "n": name, "props": props, "p": path}]}, meta)
            else:
                path = step.replace("*", rng.choice(["hello-world", "relay-ssr", "notes-on-sqlite"]))
                store.ingest(site, {"w": width, **ids, "items": [
                    {"t": "pv", "p": path, "r": ref if k == 0 else f"{site.url}/", **({"new": True} if k == 0 else {})},
                    {"t": "leave", "p": path, "ms": gaps[k] * 1000}]}, meta)
                pages.append((when, path))
    return pages


def _recording(at: datetime, site) -> list:
    """A tiny but real rrweb recording: a page with a heading, then a scroll."""
    t = int(at.timestamp() * 1000)
    doc = {"type": 0, "childNodes": [
        {"type": 1, "name": "html", "publicId": "", "systemId": "", "id": 2},
        {"type": 2, "tagName": "html", "attributes": {}, "id": 3, "childNodes": [
            {"type": 2, "tagName": "head", "attributes": {}, "id": 4, "childNodes": [
                {"type": 2, "tagName": "style", "attributes": {"_cssText": "body{font:20px system-ui;padding:40px;"
                 "background:#0e1116;color:#d6dde6}h1{color:#7cc4fa}"}, "childNodes": [], "id": 5}]},
            {"type": 2, "tagName": "body", "attributes": {}, "id": 6, "childNodes": [
                {"type": 2, "tagName": "h1", "attributes": {}, "id": 7,
                 "childNodes": [{"type": 3, "textContent": f"Demo recording on {site.id}", "id": 8}]},
                {"type": 2, "tagName": "p", "attributes": {}, "id": 9,
                 "childNodes": [{"type": 3, "textContent": "A visitor who accepted tracking moved the mouse here.", "id": 10}]}]}]}],
        "id": 1}
    events = [{"type": 4, "data": {"href": f"{site.url}/", "width": 1280, "height": 720}, "timestamp": t},
              {"type": 2, "data": {"node": doc, "initialOffset": {"left": 0, "top": 0}}, "timestamp": t + 10}]
    for i in range(1, 30):
        events.append({"type": 3, "data": {"source": 1, "positions": [
            {"x": 100 + i * 25, "y": 120 + (i % 7) * 30, "id": 6, "timeOffset": 0}]}, "timestamp": t + i * 200})
    return events
