"""Who is behind an address, beyond its country and network.

Three sources, none of which sends a visitor's address to a lookup service:

- **Reverse DNS.** The name an address's owner gives it (`crawl-66-249-66-1.googlebot.com`,
  `c-73-22-1-9.hsd1.il.comcast.net`), asked of the system's resolver with a short timeout.
  Names a crawler would have are also resolved forward, and must lead back to the same
  address: that is how Google, Bing, Apple and others say to tell their crawlers from
  impostors. Only addresses that are stored anyway are looked up.
- **Published ranges**, bundled into one table at deploy time (`infra/deploy.py intel`):
  the crawlers and monitors that publish theirs, cloud providers, iCloud Private Relay's
  egress, Tor exit relays, VPN servers and providers' networks, Starlink, Spamhaus DROP
  and recent attackers (blocklist.de, DShield). Answered from a memory-mapped file, no
  network.
- **RDAP**, only when an administrator asks about one address in the console: who the
  regional registry says holds its block.

Reverse DNS answers are cached in memory and in the table (`ip|<address>`), so an address
costs a DNS round trip about once a week.
"""
from __future__ import annotations

import ipaddress
import json
import mmap
import os
import re
import secrets
import socket
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime

from . import classify

DAY = 86400


def canonical(ip) -> str | None:
    """An address as one string per address: IPv4-mapped IPv6 as IPv4, IPv6 compressed.
    None for anything that isn't an address."""
    try:
        addr = ipaddress.ip_address(str(ip).strip())
    except ValueError:
        return None
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return str(addr)


# ---------------------------------------------------------------- DNS

PTR, CNAME, A, AAAA = 12, 5, 1, 28
NXDOMAIN = 3
HOST = re.compile(r"(?=.{1,253}$)[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?(?:\.[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?)*")


class DNSError(Exception):
    pass


def nameservers(path: str = "/etc/resolv.conf") -> tuple[str, ...]:
    """The system's resolvers (on Lambda, AWS's own), else two public ones."""
    found = []
    try:
        with open(path) as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "nameserver" and canonical(parts[1]):
                    found.append(parts[1])
    except OSError:
        pass
    return tuple(found[:3]) or ("1.1.1.1", "8.8.8.8")


NAMESERVERS = nameservers()


def _encode(name: str) -> bytes:
    out = b""
    for label in name.rstrip(".").split("."):
        raw = label.encode("ascii", "strict")
        if not 0 < len(raw) < 64:
            raise DNSError(f"bad name {name!r}")
        out += bytes([len(raw)]) + raw
    return out + b"\0"


def _name(buf: bytes, off: int) -> tuple[str, int]:
    """A name in a DNS message, following compression pointers. Returns it and the
    offset just past it where it started."""
    labels, end, hops = [], None, 0
    while True:
        if off >= len(buf):
            raise DNSError("name runs past the message")
        n = buf[off]
        if n & 0xC0 == 0xC0:
            if off + 1 >= len(buf):
                raise DNSError("short pointer")
            if end is None:
                end = off + 2
            off = ((n & 0x3F) << 8) | buf[off + 1]
            hops += 1
            if hops > 32:
                raise DNSError("pointer loop")
            continue
        if n & 0xC0:
            raise DNSError("bad label")
        off += 1
        if n == 0:
            break
        labels.append(buf[off:off + n].decode("ascii", "replace"))
        off += n
    return ".".join(labels), end if end is not None else off


def parse(buf: bytes) -> tuple[int, list[tuple[int, str]]]:
    """(response code, [(type, value)]) of a DNS response: a name for PTR and CNAME
    records, an address for A and AAAA. Other records are skipped."""
    if len(buf) < 12:
        raise DNSError("short message")
    _, flags, qd, an, _, _ = struct.unpack_from(">HHHHHH", buf)
    if not flags & 0x8000:
        raise DNSError("not a response")
    if flags & 0x0200:
        raise DNSError("truncated")
    off = 12
    for _ in range(qd):
        _, off = _name(buf, off)
        off += 4
    out = []
    for _ in range(an):
        _, off = _name(buf, off)
        if off + 10 > len(buf):
            raise DNSError("short record")
        rtype, _, _, size = struct.unpack_from(">HHIH", buf, off)
        off += 10
        data = buf[off:off + size]
        if rtype in (PTR, CNAME):
            out.append((rtype, _name(buf, off)[0]))
        elif rtype in (A, AAAA) and len(data) in (4, 16):
            out.append((rtype, str(ipaddress.ip_address(data))))
        off += size
    return flags & 0xF, out


def query(name: str, qtype: int, timeout: float = 1.0, servers: tuple[str, ...] | None = None):
    """One question over UDP, to the first resolver and then the second, within
    `timeout` seconds in all. Raises DNSError when neither answers."""
    servers = (servers or NAMESERVERS)[:2]
    question = _encode(name) + struct.pack(">HH", qtype, 1)
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    for i, server in enumerate(servers):
        left = deadline - time.monotonic()
        if left < 0.05:
            break
        until = time.monotonic() + left / (len(servers) - i)
        tid = secrets.randbits(16)
        packet = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0) + question
        try:
            with socket.socket(socket.AF_INET6 if ":" in server else socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((server, 53))
                s.send(packet)
                while True:
                    s.settimeout(max(0.01, until - time.monotonic()))
                    buf = s.recv(4096)
                    if len(buf) >= 2 and int.from_bytes(buf[:2], "big") == tid:
                        return parse(buf)
        except (OSError, DNSError) as e:  # socket.timeout is an OSError
            last = e
    raise DNSError(str(last or "no resolver answered"))


def reverse(ip: str, timeout: float = 1.0) -> str | None:
    """The address's PTR name; "" if it has none; None if DNS didn't say in time."""
    try:
        name = ipaddress.ip_address(ip).reverse_pointer
    except ValueError:
        return ""
    try:
        rcode, answers = query(name, PTR, timeout)
    except DNSError:
        return None
    if rcode == NXDOMAIN:
        return ""
    if rcode != 0:
        return None
    for rtype, value in answers:
        if rtype == PTR:
            host = value.lower().rstrip(".")
            return host if HOST.fullmatch(host) else ""
    return ""


def forward(host: str, ip: str, timeout: float = 1.0) -> bool | None:
    """Whether `host` resolves to `ip`: the second half of forward-confirmed reverse DNS.
    None if DNS didn't say in time."""
    addr = ipaddress.ip_address(ip)
    try:
        rcode, answers = query(host, AAAA if addr.version == 6 else A, timeout)
    except DNSError:
        return None
    if rcode == NXDOMAIN:
        return False
    if rcode != 0:
        return None
    return any(ipaddress.ip_address(v) == addr for t, v in answers if t in (A, AAAA))


# Second-level labels under which a country code's registrations sit (example.co.uk).
SECOND_LEVEL = frozenset({"co", "com", "net", "org", "ne", "or", "ac", "ad", "gov", "edu", "go", "gr", "ltd",
                          "plc", "sch", "nic", "mil", "gob", "info", "biz", "web", "nom", "res", "in"})


def domain_of(host: str) -> str:
    """The organisation's domain in a host name, near enough: `c-73-1.hsd1.il.comcast.net`
    -> `comcast.net`, `x.bb.example.co.uk` -> `example.co.uk`."""
    labels = (host or "").split(".")
    if len(labels) < 2:
        return host or ""
    n = 3 if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in SECOND_LEVEL else 2
    return ".".join(labels[-n:])


# ---------------------------------------------------------------- published ranges

MAGIC = b"CLRANGES"


def _interval(text: str) -> tuple[int, int, int]:
    """(IP version, first, last) of a CIDR, a bare address, or `first-last`."""
    text = text.strip()
    if "-" in text and "/" not in text:
        a, b = (ipaddress.ip_address(p.strip()) for p in text.split("-", 1))
        if a.version != b.version or int(b) < int(a):
            raise ValueError(text)
        return a.version, int(a), int(b)
    net = ipaddress.ip_network(text, strict=False)
    return net.version, int(net.network_address), int(net.broadcast_address)


def build_ranges(entries, built: str = "", notes: dict | None = None) -> bytes:
    """One table from (tag, range) pairs, where any address maps to every tag whose
    ranges hold it. Overlapping ranges are cut at their edges, and neighbouring pieces
    with the same tags joined, so a lookup is one binary search.

    Layout: MAGIC, a 4-byte header length, the JSON header (the tag combinations), then
    IPv4 rows (first, last: 4 bytes each; combination: 2) and IPv6 rows (16, 16, 2),
    each table sorted and without overlaps."""
    rows: dict[int, list[tuple[int, int, int]]] = {4: [], 6: []}
    tag_ids: dict[str, int] = {}
    for tag, text in entries:
        try:
            version, lo, hi = _interval(text)
        except ValueError:
            continue
        rows[version].append((lo, hi, tag_ids.setdefault(tag, len(tag_ids))))
    combos: dict[tuple[int, ...], int] = {}
    tables: dict[int, list[tuple[int, int, int]]] = {}
    for version, ranges in rows.items():
        events = sorted([(lo, 1, t) for lo, _, t in ranges] + [(hi + 1, -1, t) for _, hi, t in ranges])
        active: dict[int, int] = {}
        segments: list[tuple[int, int, int]] = []
        i = 0
        while i < len(events):
            at = events[i][0]
            while i < len(events) and events[i][0] == at:
                _, step, t = events[i]
                n = active.get(t, 0) + step
                if n:
                    active[t] = n
                else:
                    active.pop(t, None)
                i += 1
            if active and i < len(events):
                combo = combos.setdefault(tuple(sorted(active)), len(combos))
                end = events[i][0] - 1
                if segments and segments[-1][2] == combo and segments[-1][1] + 1 == at:
                    segments[-1] = (segments[-1][0], end, combo)
                else:
                    segments.append((at, end, combo))
        tables[version] = segments
    if len(combos) >= 1 << 16:
        raise ValueError("too many tag combinations")
    names = {i: tag for tag, i in tag_ids.items()}
    header = json.dumps({"version": 1, "built": built, "notes": notes or {},
                         "combos": [[names[t] for t in key] for key in sorted(combos, key=combos.get)],
                         "v4": len(tables[4]), "v6": len(tables[6])}, separators=(",", ":")).encode()
    out = [MAGIC, struct.pack(">I", len(header)), header]
    out += [struct.pack(">IIH", lo, hi, c) for lo, hi, c in tables[4]]
    out += [lo.to_bytes(16, "big") + hi.to_bytes(16, "big") + struct.pack(">H", c) for lo, hi, c in tables[6]]
    return b"".join(out)


class Ranges:
    """The table `build_ranges` writes, read in place (memory-mapped when from a file)."""

    def __init__(self, data, path: str = ""):
        if bytes(data[:len(MAGIC)]) != MAGIC:
            raise ValueError(f"{path or 'ranges'}: not a ranges table")
        try:
            (size,) = struct.unpack_from(">I", data, len(MAGIC))
            start = len(MAGIC) + 4
            header = json.loads(bytes(data[start:start + size]))
            self.combos = [tuple(c) for c in header["combos"]]
            self.n4, self.n6 = int(header["v4"]), int(header["v6"])
        except (struct.error, KeyError, TypeError, ValueError) as e:
            raise ValueError(f"{path or 'ranges'}: bad header ({e})") from None
        self.path = path
        self.buf = data
        self.built = header.get("built", "")
        self.notes = header.get("notes", {})
        self.off4 = start + size
        self.off6 = self.off4 + 10 * self.n4
        if len(data) < self.off6 + 34 * self.n6:
            raise ValueError(f"{path or 'ranges'}: truncated")

    @classmethod
    def open(cls, path: str) -> "Ranges":
        with open(path, "rb") as f:
            return cls(mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ), path)

    def tags(self, ip: str | None) -> tuple[str, ...]:
        ip = canonical(ip)
        if not ip:
            return ()
        addr = ipaddress.ip_address(ip)
        if addr.version == 4:
            key, n, base, width = int(addr), self.n4, self.off4, 10

            def first(i):
                return struct.unpack_from(">I", self.buf, base + width * i)[0]
        else:
            key, n, base, width = addr.packed, self.n6, self.off6, 34

            def first(i):
                return bytes(self.buf[base + width * i:base + width * i + 16])
        lo, hi = 0, n
        while lo < hi:  # the last row that starts at or before the address
            mid = (lo + hi) // 2
            if first(mid) <= key:
                lo = mid + 1
            else:
                hi = mid
        if not lo:
            return ()
        at = base + width * (lo - 1)
        if addr.version == 4:
            _, last, combo = struct.unpack_from(">IIH", self.buf, at)
        else:
            last = bytes(self.buf[at + 16:at + 32])
            (combo,) = struct.unpack_from(">H", self.buf, at + 32)
        return self.combos[combo] if key <= last else ()


# ---------------------------------------------------------------- what an address is

# Tag kinds, from the ranges table: `<kind>:<name>`. tor, relay (iCloud Private Relay),
# vpn, crawler (a crawler's or monitor's published range), cloud, hosting (a datacenter
# network), isp (an access network worth naming: Starlink), drop (Spamhaus DROP), abuse
# (seen attacking lately).
KINDS = ("tor", "relay", "vpn", "crawler", "cloud", "hosting", "isp", "drop", "abuse")


@dataclass(frozen=True)
class Intel:
    host: str | None = None         # reverse DNS: a name, "" for none, None when not known
    confirmed: bool | None = None   # the name resolves back to the address (asked for crawler names)
    tags: tuple[str, ...] = ()      # from the ranges table

    def _names(self, kind: str) -> list[str]:
        return [t.split(":", 1)[1] for t in self.tags if t.startswith(kind + ":")]

    def net(self, network: str = "") -> str:
        """What kind of connection: tor, relay (iCloud Private Relay), vpn, crawler,
        hosting, or "" for an ordinary ISP or mobile network. A hosting network's name
        counts when no list says otherwise, so this works without the ranges table too."""
        kinds = {t.split(":", 1)[0] for t in self.tags}
        for kind in ("tor", "relay", "vpn", "crawler"):
            if kind in kinds:
                return kind
        if kinds & {"cloud", "hosting"} or classify.is_datacenter(network):
            return "hosting"
        return ""

    def service(self) -> str:
        """The most telling name the lists give: the crawler, the relay, the cloud region."""
        for kind in ("tor", "relay", "vpn", "crawler", "cloud", "hosting", "isp"):
            names = self._names(kind)
            if names:
                return max(names, key=len)
        return ""

    @property
    def listed(self) -> bool:
        """On Spamhaus DROP: hijacked or criminal netblocks, which nobody should route."""
        return any(t.startswith("drop:") for t in self.tags)

    @property
    def abuse(self) -> tuple[str, ...]:
        """The attack lists that name the address."""
        return tuple(self._names("abuse"))

    @property
    def domain(self) -> str:
        return domain_of(self.host) if self.host else ""


# ---------------------------------------------------------------- crawler verification


@dataclass(frozen=True)
class Proof:
    """How a crawler says to recognise its requests."""
    hosts: tuple[str, ...] = ()    # reverse DNS name ends with one, forward-confirmed
    ranges: tuple[str, ...] = ()   # in one of these published lists (`crawler:<name>` tags)
    asns: tuple[str, ...] = ()     # from one of these networks, which rent nothing to others


GOOGLE = Proof((".googlebot.com", ".google.com", ".googleusercontent.com"),
               ("Googlebot", "Google special crawlers", "Google fetchers"))
BING = Proof((".search.msn.com",), ("Bingbot",))
APPLE = Proof((".applebot.apple.com",), ("Applebot",))
META = Proof(asns=("32934",))

# By bot name, as classify.BOTS names them. Not here: crawlers that publish nothing to
# check against, and facebookexternalhit, which iMessage also sends from people's phones.
PROOFS: dict[str, Proof] = {
    "Googlebot": GOOGLE, "Google-InspectionTool": GOOGLE, "GoogleOther": GOOGLE, "Storebot-Google": GOOGLE,
    "AdsBot-Google": GOOGLE, "Google-CloudVertexBot": GOOGLE, "Google Read Aloud": GOOGLE,
    "Bingbot": BING, "BingPreview": BING,
    "Applebot": APPLE, "Applebot-Extended": APPLE,
    "DuckDuckBot": Proof(ranges=("DuckDuckBot",)),
    "DuckAssistBot": Proof(ranges=("DuckAssistBot",)),
    "UptimeRobot": Proof(ranges=("UptimeRobot",)),
    "Pingdom": Proof(ranges=("Pingdom",)),
    "GPTBot": Proof(ranges=("GPTBot",)),
    "OAI-SearchBot": Proof(ranges=("OAI-SearchBot",)),
    "ChatGPT-User": Proof(ranges=("ChatGPT-User",)),
    "PerplexityBot": Proof(ranges=("PerplexityBot",)),
    "Perplexity-User": Proof(ranges=("Perplexity-User",)),
    "CCBot": Proof((".crawl.commoncrawl.org",), ("CCBot",)),
    "Amazonbot": Proof((".crawl.amazonbot.amazon",)),
    "YandexBot": Proof((".yandex.ru", ".yandex.net", ".yandex.com")),
    "Baiduspider": Proof((".baidu.com", ".baidu.jp")),
    "PetalBot": Proof((".petalsearch.com",)),
    "SeznamBot": Proof((".seznam.cz",)),
    "Yahoo Slurp": Proof((".crawl.yahoo.net",)),
    "AhrefsBot": Proof((".ahrefs.com", ".ahrefs.net")),
    "SemrushBot": Proof((".semrush.com",)),
    "Meta-ExternalAgent": META, "Meta-ExternalFetcher": META,
    "Twitterbot": Proof(asns=("13414", "35995")),
}
CRAWLER_HOSTS = tuple(sorted({h for p in PROOFS.values() for h in p.hosts}))


def verify(bot: str, intel: Intel, asn: str = "", ranges_loaded: bool = True) -> str:
    """Whether a request that says it is crawler `bot` is: "verified", "spoofed", or ""
    when the crawler can't be checked or nothing could be learned in time. Spoofed means
    a check that could have passed didn't, and none passed."""
    proof = PROOFS.get(bot)
    if not proof:
        return ""
    failed = False
    if proof.ranges and ranges_loaded:
        if any(f"crawler:{name}" in intel.tags for name in proof.ranges):
            return "verified"
        failed = True
    if proof.asns and asn:
        if asn in proof.asns:
            return "verified"
        failed = True
    if proof.hosts and intel.host is not None:
        if intel.host.endswith(proof.hosts):
            if intel.confirmed:
                return "verified"
            failed = failed or intel.confirmed is False
        else:
            failed = True
    return "spoofed" if failed else ""


# ---------------------------------------------------------------- clocks

ZONE = re.compile(r"[A-Za-z0-9_+\-]+(?:/[A-Za-z0-9_+\-]+){0,2}")


def clock_elsewhere(browser: str, address: str, at: datetime) -> bool | None:
    """Whether a browser's time zone keeps a different time from its address's: a
    traveller, or a VPN or proxy in another country. None when either is unknown."""
    if not browser or not address or len(browser) > 64 or not ZONE.fullmatch(browser):
        return None
    if browser == address:
        return False
    try:
        from zoneinfo import ZoneInfo

        return at.astimezone(ZoneInfo(browser)).utcoffset() != at.astimezone(ZoneInfo(address)).utcoffset()
    except Exception:  # noqa: BLE001 - an unknown zone, or no tz database
        return None


# ---------------------------------------------------------------- the service


class Intelligence:
    """Ranges always (when the table is there); reverse DNS when `dns` is on, cached in
    memory and in `cache` (the table). `reverse` and `forward` can be replaced, which the
    tests and the demo do."""

    MEMO = 8192
    HIT_S = 3600         # in memory, an answer
    MISS_S = 300         # in memory, no answer in time
    KEEP_NAMED = 7 * DAY  # in the table
    KEEP_UNNAMED = 2 * DAY

    def __init__(self, ranges: Ranges | None = None, cache=None, dns: bool = False, timeout: float = 1.0,
                 reverse=reverse, forward=forward):
        self.ranges = ranges
        self.cache = cache
        self.dns = dns
        self.timeout = timeout
        self.reverse = reverse
        self.forward = forward
        self._memo: OrderedDict[str, tuple[str | None, bool | None, float]] = OrderedDict()
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, cache=None) -> "Intelligence":
        """`INTEL_RANGES` is the ranges table (skipped if missing); `INTEL_DNS=off` turns
        reverse DNS off."""
        path = os.environ.get("INTEL_RANGES", "").strip()
        ranges = None
        if path and os.path.exists(path):
            try:
                ranges = Ranges.open(path)
            except (OSError, ValueError) as e:
                print(f"intel: skipping {path}: {e}")
        return cls(ranges, cache, dns=os.environ.get("INTEL_DNS", "on").lower() not in ("off", "0", "false"))

    def tags(self, ip: str | None) -> tuple[str, ...]:
        if not self.ranges or not ip:
            return ()
        try:
            return self.ranges.tags(ip)
        except (ValueError, IndexError, struct.error):
            return ()

    def local(self, ip: str | None) -> Intel:
        """What the ranges say, with no DNS."""
        return Intel(tags=self.tags(ip))

    def lookup(self, ip: str | None, budget: float = 1.0) -> Intel:
        if not ip:
            return Intel()
        return self.lookup_many([ip], budget).get(ip) or Intel()

    def lookup_many(self, ips, budget: float = 10.0) -> dict[str, Intel]:
        """Each address (as given) -> what is known of it, waiting at most `budget`
        seconds for DNS. Addresses DNS didn't answer for in time have `host` None."""
        given = {ip: canonical(ip) for ip in ips if ip}
        wanted = sorted({c for c in given.values() if c})
        names: dict[str, tuple[str | None, bool | None]] = {}
        todo = []
        now = time.monotonic()
        with self._lock:
            for ip in wanted:
                hit = self._memo.get(ip)
                if hit and hit[2] > now:
                    names[ip] = hit[:2]
                else:
                    todo.append(ip)
        if todo and self.dns and self.cache is not None:
            try:
                found = self.cache.get_many([(f"ip|{ip}", "-") for ip in todo])
            except Exception as e:  # noqa: BLE001 - a cache miss is not a failure
                print(json.dumps({"event": "intel_cache_failed", "error": str(e)[:200]}))
                found = {}
            for ip in todo:
                item = found.get((f"ip|{ip}", "-"))
                if item is not None:
                    conf = item.get("conf")
                    names[ip] = (str(item.get("host") or ""), None if conf is None else bool(conf))
                    self._remember(ip, *names[ip])
            todo = [ip for ip in todo if ip not in names]
        if todo and self.dns:
            fresh = self._resolve(todo, budget)
            names.update(fresh)
            keep = []
            stamp = int(time.time())
            for ip, (host, conf) in fresh.items():
                self._remember(ip, host, conf)
                needs_proof = bool(host) and host.endswith(CRAWLER_HOSTS)
                if host is None or (needs_proof and conf is None):
                    continue  # nothing definite to keep
                item = {"pk": f"ip|{ip}", "sk": "-", "host": host,
                        "expires": stamp + (self.KEEP_NAMED if host else self.KEEP_UNNAMED)}
                if conf is not None:
                    item["conf"] = 1 if conf else 0
                keep.append(item)
            if keep and self.cache is not None:
                try:
                    self.cache.put_many(keep)
                except Exception as e:  # noqa: BLE001
                    print(json.dumps({"event": "intel_cache_failed", "error": str(e)[:200]}))
        out = {}
        for ip, c in given.items():
            host, conf = names.get(c, (None, None)) if c else (None, None)
            out[ip] = Intel(host, conf, self.tags(c))
        return out

    def _remember(self, ip: str, host: str | None, conf: bool | None) -> None:
        with self._lock:
            self._memo[ip] = (host, conf, time.monotonic() + (self.MISS_S if host is None else self.HIT_S))
            self._memo.move_to_end(ip)
            while len(self._memo) > self.MEMO:
                self._memo.popitem(last=False)

    def _resolve(self, ips: list[str], budget: float) -> dict[str, tuple[str | None, bool | None]]:
        deadline = time.monotonic() + budget

        def one(ip):
            left = deadline - time.monotonic()
            if left < 0.05:
                return ip, None, None
            host = self.reverse(ip, min(self.timeout, left))
            conf = None
            if host and host.endswith(CRAWLER_HOSTS):
                left = deadline - time.monotonic()
                if left >= 0.05:
                    conf = self.forward(host, ip, min(self.timeout, left))
            return ip, host, conf

        if len(ips) == 1:
            results = [one(ips[0])]
        else:
            pool = ThreadPoolExecutor(max_workers=min(32, len(ips)))
            futures = [pool.submit(one, ip) for ip in ips]
            done, _ = wait(futures, timeout=budget + 0.25)
            pool.shutdown(wait=False, cancel_futures=True)
            results = [f.result() for f in done if not f.cancelled() and f.exception() is None]
        return {ip: (host, conf) for ip, host, conf in results}

    def confirm(self, host: str, ip: str, timeout: float = 1.5) -> bool | None:
        """Forward-confirm any name, for the console's look at one address."""
        if not self.dns or not host:
            return None
        return self.forward(host, ip, timeout)


# ---------------------------------------------------------------- the registry (RDAP)

# ARIN answers for its own space and redirects to the registry that holds the rest.
RDAP = "https://rdap.arin.net/registry/ip/"
USER_AGENT = "carterlitics (+https://cartercole.net)"


def _vcard(entity: dict) -> dict:
    out: dict[str, str] = {}
    card = entity.get("vcardArray")
    for prop in (card[1] if isinstance(card, list) and len(card) > 1 and isinstance(card[1], list) else []):
        if not isinstance(prop, list) or len(prop) < 4:
            continue
        name, params, _, value = prop[:4]
        if name in ("fn", "email", "kind") and isinstance(value, str):
            out.setdefault(name, value)
        elif name == "adr" and isinstance(params, dict) and isinstance(params.get("label"), str):
            out.setdefault("address", params["label"])
    return out


def _entities(node: dict, depth: int = 0):
    for e in node.get("entities") or []:
        if isinstance(e, dict):
            yield e
            if depth < 3:
                yield from _entities(e, depth + 1)


def summarize_rdap(data: dict, registry: str = "") -> dict:
    """The parts of an RDAP IP network answer worth showing."""
    out = {"registry": registry, "handle": data.get("handle", ""), "name": data.get("name", ""),
           "type": data.get("type", ""), "country": data.get("country", ""),
           "start": data.get("startAddress", ""), "end": data.get("endAddress", ""),
           "parent": data.get("parentHandle", ""), "holder": "", "abuse": "", "registered": "", "updated": ""}
    out["cidrs"] = [f"{c.get('v4prefix') or c.get('v6prefix')}/{c.get('length')}"
                    for c in data.get("cidr0_cidrs") or [] if isinstance(c, dict)]
    holders = []
    for n, e in enumerate(_entities(data)):
        roles = e.get("roles") or []
        card = _vcard(e)
        if card.get("fn") and ("registrant" in roles or "administrative" in roles):
            # The organisation that holds it, over a maintainer or a contact (RIPE lists those first).
            rank = (0 if "registrant" in roles else 2) + (0 if card.get("kind") == "org" else 1)
            holders.append((rank, n, card["fn"], card.get("address", "")))
        if not out["abuse"] and "abuse" in roles and card.get("email"):
            out["abuse"] = card["email"]
    if holders:
        _, _, out["holder"], out["address"] = min(holders)
    for ev in data.get("events") or []:
        if isinstance(ev, dict) and ev.get("eventAction") == "registration":
            out["registered"] = ev.get("eventDate", "")
        elif isinstance(ev, dict) and ev.get("eventAction") == "last changed":
            out["updated"] = ev.get("eventDate", "")
    remarks = []
    for r in data.get("remarks") or []:
        if isinstance(r, dict):
            remarks += [d for d in r.get("description") or [] if isinstance(d, str)]
    out["remarks"] = remarks[:6]
    return out


def whois(ip: str, timeout: float = 6.0) -> dict:
    """Who the regional registry says holds the block `ip` is in, over RDAP. Sends the
    address to ARIN (and the registry it redirects to), so it runs only on request."""
    ip = canonical(ip)
    if not ip:
        return {"error": "not an address"}
    req = urllib.request.Request(RDAP + urllib.parse.quote(ip), headers={
        "Accept": "application/rdap+json, application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            data = json.loads(res.read(2_000_000))
            registry = urllib.parse.urlsplit(res.geturl()).hostname or ""
    except urllib.error.HTTPError as e:
        return {"error": f"registry answered {e.code}"}
    except (OSError, ValueError) as e:
        return {"error": f"registry unreachable: {str(e)[:120]}"}
    return summarize_rdap(data, registry) if isinstance(data, dict) else {"error": "unexpected answer"}
