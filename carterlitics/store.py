"""What gets written, and what the console reads back.

Every site shares one table and one bucket; every key starts with the site, so sites
never see each other's data. The layout:

    c|<site>|<day>|web   <dim>#<value>  n        browser counters, one item per value
    c|<site>|<day>|srv   <dim>#<value>  n        server (CloudFront log) counters
    c|<site>|<day>|web   uv             h = {…}  that day's visitor hashes (a string set)
    l|<site>|<day>|<k>   <ts>#<rand>    …        logs: pv (consented page views), ev, err,
                                                 hum and bot (server requests)
    s|<site>|<day>       <session id>   …        a consented session, keyed by its first day
    g|<site>             <group id>     …        an error group: first/last seen, a sample
    o|<site>|<group>     <ts>#<rand>    …        that group's occurrences
    salt|<day>           -              salt     the day's visitor-hash salt, gone in 2 days
    f|<s3 key>           -                       a CloudFront log file already counted

and in the bucket, `rec/<site>/<session>/<seq>.json.gz`, one gzipped JSON array of
rrweb events per chunk.

Counters are bumped with an atomic ADD, so nothing is ever aggregated after the fact
and a report is a handful of queries. Every item carries `expires` for the table's TTL.

Unique visitors are counted the way Plausible does it: a hash of a random daily salt,
the site, the IP address and the user agent. The salt is thrown away after two days, so
a hash can't be traced back to an address once the day is over, and no cookie is used.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import secrets
import statistics
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from . import classify
from .classify import Bot, clean_path, text, valid_id, valid_name
from .geo import Geo, Geolocator

DAY = 86400
MAX_ITEMS = 50                 # per browser batch
MAX_CHUNK_BYTES = 2_000_000    # one recording chunk, as JSON, before gzip
MAX_CHUNKS = 100_000
MAX_PROPS = 2000               # an event's properties, as JSON
TOP = 25


def now() -> datetime:
    return datetime.now(timezone.utc)


def day_of(at: datetime) -> str:
    return at.strftime("%Y-%m-%d")


def days_back(days: int, today: datetime | None = None) -> list[str]:
    """Oldest first, ending today (UTC)."""
    today = today or now()
    return [day_of(today - timedelta(days=d)) for d in range(days - 1, -1, -1)]


def stamp(at: datetime, seq: int | None = None) -> str:
    """A sort key that sorts by time, unique enough to never collide. `seq` keeps the
    items of one batch in the order they happened, though they share a millisecond:
    a page view, then the click on it, is the order a funnel needs."""
    order = f"{seq:02d}" if seq is not None else ""
    return f"{at.strftime('%Y-%m-%dT%H:%M:%S.%f')[:23]}Z#{order}{secrets.token_hex(3)}"


def iso(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- keys


def web(site: str, day: str) -> str:
    return f"c|{site}|{day}|web"


def srv(site: str, day: str) -> str:
    return f"c|{site}|{day}|srv"


def log_pk(site: str, day: str, kind: str) -> str:
    return f"l|{site}|{day}|{kind}"


def sessions_pk(site: str, day: str) -> str:
    return f"s|{site}|{day}"


def groups_pk(site: str) -> str:
    return f"g|{site}"


def occurrences_pk(site: str, gid: str) -> str:
    return f"o|{site}|{gid}"


def rec_prefix(site: str, session: str) -> str:
    return f"rec/{site}/{session.lower()}/"


LOG_KINDS = ("pv", "ev", "err", "hum", "bot")


# ---------------------------------------------------------------- inputs


@dataclass(frozen=True)
class Meta:
    """What the HTTP layer knows about a browser request, from CloudFront's headers."""
    ip: str | None = None
    user_agent: str | None = None
    country: str = ""
    asn: str = ""
    city: str = ""
    opted_out: bool = False  # Do Not Track or Global Privacy Control


@dataclass(frozen=True)
class LogLine:
    """One CloudFront access log record, in the fields this uses."""
    at: datetime
    host: str
    ip: str
    method: str
    path: str
    query: str
    status: int
    referrer: str
    user_agent: str
    bytes: int = 0
    edge: str = ""
    seconds: float = 0.0
    country: str = ""
    asn: str = ""


# ---------------------------------------------------------------- errors


NUMBERS = re.compile(r"\b0x[0-9a-f]+\b|\b\d+\b", re.I)
QUOTED = re.compile(r"(['\"`]).{1,80}?\1")
FRAME = re.compile(r"(?:at\s+)?(?:([^\s(@]+)\s*[(@])?\(?((?:https?|file|webpack|blob):[^\s)]+?)(?::\d+){1,2}\)?\s*$")


def top_frame(stack: str) -> str:
    """`function@file` of the first stack frame, without line numbers, which move with
    every deploy."""
    for line in (stack or "").splitlines()[:12]:
        m = FRAME.search(line.strip())
        if m:
            func, url = m.group(1) or "", m.group(2)
            url = url.split("?")[0]
            # Content-hashed bundle names change on every build; group across them.
            url = re.sub(r"[-.][0-9a-f]{8,}(?=\.\w+$)", "", url.rsplit("/", 1)[-1])
            return f"{func}@{url}"
    return ""


def group_id(kind: str, name: str, message: str, stack: str, source: str) -> str:
    """Occurrences of one bug share an id: same kind, error type and message shape
    (numbers and quoted values stripped), and the same top frame."""
    shape = QUOTED.sub("…", NUMBERS.sub("N", message or ""))[:200]
    where = top_frame(stack) or (source or "").split("?")[0].rsplit("/", 1)[-1]
    raw = "\x1f".join([kind, name or "", shape, where])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


ERROR_KINDS = {"error", "rejection", "resource", "csp", "manual", "console"}


# ---------------------------------------------------------------- store


class Store:
    """With no table (or, for recordings, no bucket) writes are dropped and reports are
    empty, so the API runs before anything is provisioned."""

    def __init__(self, cfg, table=None, blobs=None, geo: Geolocator | None = None):
        self.cfg = cfg
        self.table = table
        self.blobs = blobs
        self.geo = geo or Geolocator([])
        self._salt: tuple[str, str] | None = None

    def keep(self, what: str) -> int:
        return int(time.time()) + self.cfg.retention[what] * DAY

    def locate(self, ip: str | None, country: str = "", asn: str = "", city: str = "") -> Geo:
        """CloudFront's answer first (it knows the address best), the database for the rest."""
        edge = Geo(country.upper()[:8], asn.upper().removeprefix("AS")[:12], "", city[:80])
        db = self.geo.lookup(ip) if ip else Geo()
        if edge.asn and db.asn and edge.asn != db.asn:
            db = Geo(db.country, "", "", db.city)  # its network name belongs to another AS
        return edge.merge(db)

    # -- unique visitors

    def salt(self, day: str) -> str:
        if self._salt and self._salt[0] == day:
            return self._salt[1]
        fresh = secrets.token_hex(16)
        item = {"pk": f"salt|{day}", "sk": "-", "salt": fresh, "expires": int(time.time()) + 2 * DAY}
        if not self.table.put_new(item):
            got = self.table.get(f"salt|{day}", "-")
            fresh = got["salt"] if got else fresh
        self._salt = (day, fresh)
        return fresh

    def visitor_hash(self, site: str, day: str, ip: str | None, ua: str | None) -> str:
        raw = f"{self.salt(day)}|{site}|{ip or ''}|{ua or ''}"
        return hashlib.sha256(raw.encode()).hexdigest()[:12]

    # -- browser batches

    def ingest(self, site, batch: dict, meta: Meta) -> dict:
        """One POST from c.js: page views, page leaves, events and errors.

        Identifiers (visitor, session, fingerprint, user) are only present when the
        visitor consented, and only then are the IP address and user agent stored
        against them. Everything else lands in counters."""
        if not self.table or not isinstance(batch, dict):
            return {"stored": 0}
        items = batch.get("items")
        if not isinstance(items, list):
            return {"stored": 0}

        at = now()
        day = day_of(at)
        sid, vid, fp = batch.get("sid"), batch.get("vid"), batch.get("fp")
        consented = valid_id(sid) and valid_id(vid) and (fp is None or valid_id(fp))
        if not consented:
            sid = vid = fp = None
        uid = text(batch.get("uid"), 120) if consented else ""
        start_day = batch.get("sd") if consented and isinstance(batch.get("sd"), str) else day
        if start_day not in days_back(3, at):
            start_day = day

        ua = meta.user_agent or ""
        bot = classify.bot_of(ua)
        if not bot and batch.get("wd") is True:
            bot = Bot("webdriver", "automation")
        geo = self.locate(meta.ip, meta.country, meta.asn, meta.city)
        browser, system = classify.browser_of(ua)
        device = classify.device_of(batch.get("w"))
        own = site.hosts

        counts: Counter = Counter()
        logs: list[dict] = []
        session: Counter = Counter()
        new_session: dict | None = None
        groups: list[tuple[str, dict]] = []
        stored = 0
        viewed = False
        W = web(site.id, day)

        def who() -> dict:
            if not consented:
                return {}
            return {"vid": vid, "sid": sid, "fp": fp or "", "uid": uid, "ip": text(meta.ip, 64),
                    "ua": text(ua), "country": geo.country, "asn": geo.asn, "network": geo.network,
                    "city": geo.city}

        for seq, item in enumerate(items[:MAX_ITEMS]):
            if not isinstance(item, dict):
                continue
            kind = item.get("t")
            path = clean_path(item.get("p"), site.exclude)

            if kind == "pv":
                # `dup`: consent was given on a page already counted, so it is only logged.
                dup = item.get("dup") is True
                if meta.opted_out or not path or (dup and not consented):
                    continue
                if bot:
                    counts[(W, "jsbot")] += 1
                    continue
                ref = classify.referrer_of(item.get("r"), own)
                if not dup:
                    for sk in ("pv", f"path#{path}", f"ref#{ref}", f"dev#{device}",
                               f"cty#{geo.country or '??'}", f"br#{browser}", f"os#{system}"):
                        counts[(W, sk)] += 1
                    viewed = True
                stored += 1
                if consented:
                    logs.append({"pk": log_pk(site.id, day, "pv"), "sk": stamp(at, seq), "path": path,
                                 "ref": text(item.get("r"), 300), "browser": browser, "os": system,
                                 "device": device, **who(), "expires": self.keep("visits")})
                    session["pages"] += 1
                    if item.get("new") is True:
                        new_session = {"pk": sessions_pk(site.id, start_day), "sk": sid,
                                       "started": iso(at), "last": iso(at), "path": path,
                                       "ref": classify.referrer_of(item.get("r"), own),
                                       "browser": browser, "os": system, "device": device, **who(),
                                       "pages": 0, "events": 0, "errors": 0, "ms": 0,
                                       "expires": self.keep("recordings")}

            elif kind == "leave":
                if meta.opted_out or bot or not path:
                    continue
                ms = item.get("ms")
                if not isinstance(ms, int) or isinstance(ms, bool) or not 0 < ms <= 3_600_000:
                    continue
                counts[(W, "ms")] += ms
                counts[(W, f"ms#{path}")] += ms
                session["ms"] += ms
                stored += 1

            elif kind == "ev":
                name = item.get("n")
                if meta.opted_out or bot or not valid_name(name):
                    continue
                props = item.get("props")
                props = props if isinstance(props, dict) else {}
                blob = json.dumps(props, separators=(",", ":"), default=str)
                if len(blob) > MAX_PROPS:
                    blob = json.dumps({"_truncated": True})
                counts[(W, "ev")] += 1
                counts[(W, f"ev#{name}")] += 1
                logs.append({"pk": log_pk(site.id, day, "ev"), "sk": stamp(at, seq), "name": name,
                             "props": blob, "path": path or "", "browser": browser,
                             "country": geo.country, "source": "browser", **who(),
                             "expires": self.keep("events")})
                session["events"] += 1
                stored += 1

            elif kind == "err":
                if bot:
                    continue
                err = self._error(item)
                if not err:
                    continue
                gid = group_id(err["kind"], err["name"], err["message"], err["stack"], err["src"])
                counts[(W, "err")] += 1
                counts[(W, f"eg#{gid}")] += 1
                occurrence = {**err, "path": path or "", "browser": browser, "os": system,
                              "country": geo.country, **who()}
                if not consented:
                    occurrence["ua"] = text(ua)  # diagnostics need it; no identifier goes with it
                logs.append({"pk": occurrences_pk(site.id, gid), "sk": stamp(at, seq), **occurrence,
                             "expires": self.keep("errors")})
                logs.append({"pk": log_pk(site.id, day, "err"), "sk": stamp(at, seq), "gid": gid,
                             **occurrence, "expires": self.keep("errors")})
                groups.append((gid, {**err, "path": path or ""}))
                session["errors"] += 1
                stored += 1

        if viewed:
            try:
                self.table.add_to_set(W, "uv", "h", {self.visitor_hash(site.id, day, meta.ip, ua)},
                                      self.keep("counts"))
            except Exception as e:  # noqa: BLE001 - a full set must not lose the page view
                print(json.dumps({"event": "uv_set_failed", "site": site.id, "error": str(e)[:200]}))

        self.table.add_many(dict(counts), self.keep("counts"))
        if logs:
            self.table.put_many(logs)
        for gid, err in groups:
            self.table.update(groups_pk(site.id), gid, add={"n": 1},
                              set_={"last": iso(at), "last_path": err["path"], "expires": self.keep("errors")},
                              set_if_absent={"first": iso(at), "kind": err["kind"], "name": err["name"],
                                             "message": err["message"], "stack": err["stack"],
                                             "src": err["src"]})
        if consented:
            if new_session:
                self.table.put_new(new_session)
            if session:
                self.table.update(sessions_pk(site.id, start_day), sid, add=dict(session),
                                  set_={"last": iso(at), **({"uid": uid} if uid else {})}, must_exist=True)
        return {"stored": stored}

    @staticmethod
    def _error(item: dict) -> dict | None:
        kind = item.get("k") if item.get("k") in ERROR_KINDS else "error"
        message = text(item.get("m"), 500).strip()
        if not message:
            return None

        def num(v):
            return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 10_000_000 else 0

        return {"kind": kind, "name": text(item.get("name"), 80), "message": message,
                "stack": text(item.get("st"), 4000), "src": text(item.get("src"), 300),
                "line": num(item.get("ln")), "col": num(item.get("col"))}

    # -- events from a site's own server

    def server_events(self, site, events: list, meta: Meta) -> int:
        """Events a site's backend sends with its server key: a sign-up, a purchase.
        Each may carry the visitor's IP and user agent, which the backend saw."""
        if not self.table or not isinstance(events, list):
            return 0
        at, stored = now(), 0
        day = day_of(at)
        counts: Counter = Counter()
        logs = []
        for seq, e in enumerate(events[:MAX_ITEMS]):
            if not isinstance(e, dict) or not valid_name(e.get("name")):
                continue
            props = e.get("props") if isinstance(e.get("props"), dict) else {}
            blob = json.dumps(props, separators=(",", ":"), default=str)
            ip, ua = text(e.get("ip"), 64) or None, text(e.get("ua"))
            geo = self.locate(ip)
            counts[(web(site.id, day), "ev")] += 1
            counts[(web(site.id, day), f"ev#{e['name']}")] += 1
            logs.append({"pk": log_pk(site.id, day, "ev"), "sk": stamp(at, seq), "name": e["name"],
                         "props": blob if len(blob) <= MAX_PROPS else '{"_truncated":true}',
                         "path": clean_path(e.get("path")) or "", "source": "server",
                         "uid": text(e.get("uid"), 120), "ip": ip or "", "ua": ua,
                         "browser": classify.browser_of(ua)[0] if ua else "", "country": geo.country,
                         "asn": geo.asn, "network": geo.network, "expires": self.keep("events")})
            stored += 1
        self.table.add_many(dict(counts), self.keep("counts"))
        self.table.put_many(logs)
        return stored

    # -- session recordings

    def chunk(self, site, data: dict, meta: Meta) -> bool:
        if not self.table or not self.blobs or not isinstance(data, dict):
            return False
        session, visitor, seq, events = (data.get(k) for k in ("sid", "vid", "seq", "events"))
        if not valid_id(session) or not valid_id(visitor) or not isinstance(seq, int) \
                or isinstance(seq, bool) or not 0 <= seq < MAX_CHUNKS:
            raise ValueError("invalid identifier")
        if not isinstance(events, list) or not events:
            raise ValueError("invalid recording chunk")
        blob = json.dumps(events, separators=(",", ":"))
        if len(blob) > MAX_CHUNK_BYTES:
            raise ValueError("recording chunk too large")
        # mtime=0: the same chunk always compresses to the same bytes.
        self.blobs.put(f"{rec_prefix(site.id, session)}{seq:06d}.json.gz",
                       gzip.compress(blob.encode(), mtime=0), "application/gzip")
        if seq == 0:
            at = now()
            start_day = data.get("sd") if data.get("sd") in days_back(3, at) else day_of(at)
            # The page view normally made the session already; this covers a lost beacon.
            geo = self.locate(meta.ip, meta.country, meta.asn, meta.city)
            self.table.update(sessions_pk(site.id, start_day), session, set_={"rec": 1},
                              set_if_absent={"started": iso(at), "last": iso(at), "vid": visitor,
                                             "ip": text(meta.ip, 64), "ua": text(meta.user_agent),
                                             "country": geo.country, "network": geo.network,
                                             "path": clean_path(data.get("p"), site.exclude) or "",
                                             "expires": self.keep("recordings")})
        return True

    def recording(self, site, session) -> str | None:
        """Every chunk in order, as one JSON array. Each was validated when stored: no re-parse."""
        if not self.blobs or not valid_id(session):
            return None
        keys = self.blobs.keys(rec_prefix(site.id, session))
        if not keys:
            return None
        with ThreadPoolExecutor(max_workers=8) as pool:
            chunks = list(pool.map(lambda k: gzip.decompress(self.blobs.get(k)).decode(), keys))
        inner = [c[1:-1] for c in chunks if len(c) > 2]
        return f"[{','.join(inner)}]" if inner else None

    # -- server logs

    def ingest_log(self, lines, source: str = "") -> dict:
        """CloudFront access log records for any of the sites. Every request that isn't
        for an asset is counted and logged, people and bots alike."""
        if not self.table:
            return {"lines": 0}
        if source and not self.table.put_new({"pk": f"f|{source}", "sk": "-",
                                              "expires": int(time.time()) + 14 * DAY}):
            return {"lines": 0, "duplicate": True}
        counts: Counter = Counter()
        logs: list[dict] = []
        seen = skipped = 0
        for line in lines:
            seen += 1
            site = self.cfg.site_for_host(line.host)
            if not site or line.path.startswith("/_c/") or classify.is_asset(line.path):
                skipped += 1
                continue
            day = day_of(line.at)
            S = srv(site.id, day)
            bot = classify.bot_of(line.user_agent, line.path)
            geo = self.locate(line.ip, line.country, line.asn)
            datacenter = classify.is_datacenter(geo.network)
            counts[(S, "req")] += 1
            counts[(S, f"ss#{line.status}")] += 1
            path = clean_path(line.path)
            shown = path or classify.dim(line.path, 200)
            if bot:
                counts[(S, "bot")] += 1
                counts[(S, f"sb#{bot.name}")] += 1
                counts[(S, f"sc#{bot.category}")] += 1
                counts[(S, f"sp#{shown}")] += 1
                counts[(S, f"sip#{line.ip}")] += 1
                if geo.network:
                    counts[(S, f"sasn#{classify.dim(geo.network, 80)}")] += 1
            else:
                counts[(S, "hum")] += 1
                if path:
                    counts[(S, f"shp#{path}")] += 1
                counts[(S, f"scty#{geo.country or '??'}")] += 1
                if datacenter:
                    counts[(S, "dc")] += 1
            ip = line.ip
            if site.server_ip == "truncate":
                ip = truncate_ip(ip)
            logs.append({
                "pk": log_pk(site.id, day, "bot" if bot else "hum"), "sk": stamp(line.at),
                "at": iso(line.at), "ip": ip, "method": line.method[:10], "path": classify.dim(line.path, 300),
                "query": classify.dim(redact_query(line.query), 300), "status": line.status,
                "ref": classify.dim(line.referrer, 300), "ua": classify.dim(line.user_agent, 500),
                "bytes": line.bytes, "edge": line.edge[:24], "ms": int(line.seconds * 1000),
                "country": geo.country, "asn": geo.asn, "network": geo.network,
                "bot": bot.name if bot else "", "cat": bot.category if bot else "",
                "dc": 1 if datacenter else 0, "expires": self.keep("server"),
            })
        self.table.add_many(dict(counts), self.keep("counts"))
        self.table.put_many(logs)
        return {"lines": seen, "stored": len(logs), "skipped": skipped}

    # -- reports

    def report(self, site, days: int) -> dict:
        """Everything the console's dashboards show, for one site over `days` days."""
        keys = days_back(days)
        out = {"site": site.id, "configured": self.table is not None, "days": days, "series": [],
               "totals": {}, "top": {}, "groups": []}
        if not self.table:
            return out
        with ThreadPoolExecutor(max_workers=16) as pool:
            webs = list(pool.map(lambda d: self.table.query(web(site.id, d)), keys))
            srvs = list(pool.map(lambda d: self.table.query(srv(site.id, d)), keys))
            groups = pool.submit(self.table.query, groups_pk(site.id))
            groups = groups.result()

        dims: dict[str, Counter] = {}
        totals: Counter = Counter()
        for day, w_items, s_items in zip(keys, webs, srvs):
            row = {"day": day, "pv": 0, "uv": 0, "ms": 0, "jsbot": 0, "ev": 0, "err": 0, "req": 0,
                   "hum": 0, "bot": 0, "dc": 0}
            for item in w_items + s_items:
                sk = item["sk"]
                if sk == "uv":
                    row["uv"] = len(item.get("h") or ())
                    continue
                if "#" not in sk:
                    if sk in row:
                        row[sk] += item.get("n", 0)
                    continue
                kind, value = sk.split("#", 1)
                dims.setdefault(kind, Counter())[value] += item.get("n", 0)
            for k, v in row.items():
                if k != "day":
                    totals[k] += v
            out["series"].append(row)

        def top(kind: str, n: int = TOP) -> list[dict]:
            rows = sorted((dims.get(kind) or {}).items(), key=lambda kv: (-kv[1], kv[0]))[:n]
            return [{"name": k, "value": v} for k, v in rows]

        paths = top("path", 50)
        for row in paths:
            # Engaged time per view: visible time only, summed over every leave.
            ms = dims.get("ms", {}).get(row["name"], 0)
            row["avg_ms"] = round(ms / row["value"]) if ms and row["value"] else None
        out["totals"] = dict(totals)
        out["top"] = {
            "paths": paths, "referrers": top("ref"), "devices": top("dev"), "countries": top("cty"),
            "browsers": top("br"), "systems": top("os"), "events": top("ev", 50),
            "bots": top("sb", 50), "categories": top("sc"), "statuses": top("ss"),
            "bot_paths": top("sp", 50), "bot_ips": top("sip", 50), "bot_networks": top("sasn", 30),
            "server_paths": top("shp", 50), "server_countries": top("scty"),
        }
        info = {g["sk"]: g for g in groups}
        out["groups"] = [
            {"id": gid, "count": n, "total": info.get(gid, {}).get("n", n),
             **{k: info.get(gid, {}).get(k, "") for k in ("kind", "name", "message", "src", "first",
                                                           "last", "last_path")}}
            for gid, n in sorted((dims.get("eg") or {}).items(), key=lambda kv: -kv[1])[:100]
        ]
        return out

    def log(self, site, kind: str, days: int, limit: int) -> list[dict]:
        """The newest `limit` records of one log, across the last `days` days."""
        if not self.table or kind not in LOG_KINDS:
            return []
        out: list[dict] = []
        for day in reversed(days_back(days)):
            for item in self.table.query(log_pk(site.id, day, kind), newest_first=True,
                                         limit=limit - len(out)):
                out.append(public(item))
            if len(out) >= limit:
                break
        return out

    def sessions(self, site, days: int) -> list[dict]:
        if not self.table:
            return []
        with ThreadPoolExecutor(max_workers=8) as pool:
            parts = list(pool.map(lambda d: self.table.query(sessions_pk(site.id, d)), days_back(days)))
        rows = [{**public(i), "id": i["sk"]} for part in parts for i in part]
        return sorted(rows, key=lambda r: r.get("started", ""), reverse=True)

    def error(self, site, gid: str, limit: int = 50) -> dict | None:
        if not self.table or not re.fullmatch(r"[0-9a-f]{16}", gid or ""):
            return None
        group = self.table.get(groups_pk(site.id), gid)
        if not group:
            return None
        return {"group": {**public(group), "id": gid},
                "occurrences": [public(i) for i in
                                self.table.query(occurrences_pk(site.id, gid), newest_first=True, limit=limit)]}

    # -- funnels

    def funnels(self, site, funnels, days: int) -> dict:
        """Each funnel over the last `days` days, from the page-view and event logs.

        Only consented visitors carry a session there, so only they are counted: an
        anonymous page view isn't logged at all, and an anonymous event has nothing to
        tie it to the page before it. An event from the site's backend joins the
        latest session (or visitor) that the same user id had begun before it."""
        out = {"days": days, "funnels": [], "sessions": 0, "visitors": 0}
        hits: list[Hit] = []
        if self.table and funnels:
            kinds = sorted({"pv" if s.kind == "page" else "ev" for f in funnels for s in f.steps})
            parts = [(d, k) for d in days_back(days) for k in kinds]
            with ThreadPoolExecutor(max_workers=16) as pool:
                found = list(pool.map(lambda p: self.table.query(log_pk(site.id, p[0], p[1])), parts))
            for (_, kind), items in zip(parts, found):
                hits.extend(filter(None, (Hit.of(kind, i) for i in items)))
            hits.sort(key=lambda h: h.sk)
        out["sessions"] = len({h.sid for h in hits if h.sid})
        out["visitors"] = len({h.vid for h in hits if h.vid})
        out["funnels"] = [funnel_report(f, hits) for f in funnels]
        return out


# ---------------------------------------------------------------- funnels


class Hit(NamedTuple):
    """A page view or an event, as a funnel sees it."""
    sk: str            # the log's sort key: time, then order within a batch
    kind: str          # "page" or "event"
    value: str         # the path, or the event's name
    props: dict
    sid: str
    vid: str
    uid: str

    @property
    def at(self) -> str:
        return self.sk.split("#", 1)[0]

    @classmethod
    def of(cls, kind: str, item: dict) -> "Hit | None":
        ids = (item.get("sid") or "", item.get("vid") or "", item.get("uid") or "")
        if not any(ids):
            return None   # anonymous: nothing ties it to anything else
        if kind == "pv":
            return cls(item["sk"], "page", item.get("path") or "", {}, *ids)
        props = item.get("props")
        try:
            props = json.loads(props) if isinstance(props, str) else {}
        except ValueError:
            props = {}
        return cls(item["sk"], "event", item.get("name") or "", props if isinstance(props, dict) else {}, *ids)


def _seconds(a: str, b: str) -> float:
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()


def funnel_report(funnel, hits: list[Hit], sample: int = 8) -> dict:
    """How many journeys (sessions, or visitors) reached each step of `funnel`, in order.

    `hits` must be sorted by time. A journey's steps are matched greedily: the first
    hit that matches step 1, then the first after it that matches step 2, and so on,
    which finds the furthest step it reached. Other hits in between don't matter."""
    by_session = funnel.by == "session"
    journeys: dict[str, list[Hit]] = {}
    users: dict[str, set[str]] = {}
    backend: list[Hit] = []
    for h in hits:
        key = h.sid if by_session else h.vid
        if key:
            journeys.setdefault(key, []).append(h)
            if h.uid:
                users.setdefault(h.uid, set()).add(key)
        elif h.uid:
            backend.append(h)
    for h in backend:
        # The user's latest journey that had started by then.
        started = [(journeys[k][0].sk, k) for k in users.get(h.uid, ()) if journeys[k][0].sk <= h.sk]
        if started:
            journeys[max(started)[1]].append(h)
    n = len(funnel.steps)
    reached = [0] * n
    gaps: list[list[float]] = [[] for _ in range(n)]
    stopped: list[list[tuple[str, str, str]]] = [[] for _ in range(n)]
    for key, path in journeys.items():
        path.sort(key=lambda h: h.sk)
        times: list[str] = []
        for h in path:
            if funnel.steps[len(times)].matches(h.kind, h.value, h.props):
                times.append(h.at)
                if len(times) == n:
                    break
        if not times:
            continue
        for i in range(len(times)):
            reached[i] += 1
            if i:
                gaps[i].append(_seconds(times[i - 1], times[i]))
        if len(times) < n:
            sid = key if by_session else next((h.sid for h in reversed(path) if h.sid), "")
            stopped[len(times) - 1].append((path[-1].at, key, sid))
    steps = []
    for i, step in enumerate(funnel.steps):
        recent = sorted(stopped[i], reverse=True)[:sample]
        steps.append({"step": step.text, "kind": step.kind, "count": reached[i],
                      "median_s": round(statistics.median(gaps[i]), 1) if gaps[i] else None,
                      "dropped": [{"at": at, "key": key, "sid": sid} for at, key, sid in recent]})
    return {"name": funnel.name, "by": funnel.by, "steps": steps, "entered": reached[0],
            "completed": reached[-1], "journeys": len(journeys)}


def public(item: dict) -> dict:
    """An item as the console sees it: no keys or TTL, sets as lists."""
    out = {k: (sorted(v) if isinstance(v, set) else v) for k, v in item.items()
           if k not in ("pk", "expires")}
    sk = out.pop("sk", "")
    if "at" not in out and "#" in sk:
        out["at"] = sk.split("#", 1)[0]
    if isinstance(out.get("props"), str):
        try:
            out["props"] = json.loads(out["props"])
        except ValueError:
            pass
    return out


SECRET_PARAMS = re.compile(
    r"(?i)^(code|state|token|access_token|id_token|refresh_token|password|passwd|pass|secret|key|api_key|apikey"
    r"|signature|sig|x-amz-signature|x-amz-credential|x-amz-security-token|auth|session|session_id|sid|otp"
    r"|client_secret|code_verifier|ticket|jwt|assertion)$")


def redact_query(query: str) -> str:
    """A query string for the request log, with the values of anything credential-like
    replaced: OAuth callbacks carry codes and state, signed URLs carry signatures."""
    if not query:
        return ""
    out = []
    for part in query.split("&"):
        name, eq, _ = part.partition("=")
        out.append(f"{name}=…" if eq and SECRET_PARAMS.match(name) else part)
    return "&".join(out)


def truncate_ip(ip: str) -> str:
    """198.51.100.7 -> 198.51.100.0, and an IPv6 address to its /48."""
    if ":" in ip:
        parts = ip.split(":")
        return ":".join(parts[:3]) + "::"
    parts = ip.split(".")
    return ".".join(parts[:3] + ["0"]) if len(parts) == 4 else ip
