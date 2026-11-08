"""IP address -> country, place and network, from MaxMind-format databases (.mmdb).

A small reader for the MaxMind DB format in pure Python, so the Lambda needs no
dependencies. It reads IPinfo Lite (`country_code`, `asn`, `as_name`) and MaxMind's
GeoLite2 Country, City and ASN files (City adds the city, region and time zone); give it
several and their answers are merged.
Files are memory-mapped, so a large database costs page cache, not heap.

Format: https://maxmind.github.io/MaxMind-DB/
"""
from __future__ import annotations

import ipaddress
import mmap
import os
import struct
from dataclasses import dataclass
from functools import lru_cache

METADATA_MARKER = b"\xab\xcd\xefMaxMind.com"


@dataclass(frozen=True)
class Geo:
    country: str = ""   # ISO 3166-1 alpha-2, "US"
    asn: str = ""       # digits only, "15169"
    network: str = ""   # the AS organisation, "Google LLC"
    city: str = ""
    region: str = ""    # the first subdivision, "California"
    tz: str = ""        # IANA time zone, "America/Los_Angeles"

    def merge(self, other: "Geo") -> "Geo":
        return Geo(self.country or other.country, self.asn or other.asn,
                   self.network or other.network, self.city or other.city,
                   self.region or other.region, self.tz or other.tz)


class InvalidDatabase(Exception):
    pass


class Reader:
    def __init__(self, path: str):
        self.path = path
        with open(path, "rb") as f:
            self.buf = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        start = self.buf.rfind(METADATA_MARKER, max(0, len(self.buf) - 128 * 1024))
        if start < 0:
            raise InvalidDatabase(f"{path}: no MaxMind metadata")
        meta_start = start + len(METADATA_MARKER)
        meta, _ = self._decode(meta_start, meta_start)
        try:
            self.node_count = int(meta["node_count"])
            self.record_size = int(meta["record_size"])
            self.ip_version = int(meta["ip_version"])
        except (KeyError, TypeError, ValueError):
            raise InvalidDatabase(f"{path}: incomplete metadata") from None
        if self.record_size not in (24, 28, 32):
            raise InvalidDatabase(f"{path}: record size {self.record_size}")
        self.database_type = str(meta.get("database_type", ""))
        self.node_bytes = self.record_size * 2 // 8
        self.tree_size = self.node_bytes * self.node_count
        self.data_start = self.tree_size + 16
        self._ipv4_start = self._find_ipv4_start()

    # -- search tree

    def _record(self, node: int, bit: int) -> int:
        off = node * self.node_bytes
        b = self.buf
        if self.record_size == 24:
            o = off + bit * 3
            return (b[o] << 16) | (b[o + 1] << 8) | b[o + 2]
        if self.record_size == 28:
            if bit == 0:
                return ((b[off + 3] & 0xF0) << 20) | (b[off] << 16) | (b[off + 1] << 8) | b[off + 2]
            return ((b[off + 3] & 0x0F) << 24) | (b[off + 4] << 16) | (b[off + 5] << 8) | b[off + 6]
        return struct.unpack_from(">I", b, off + bit * 4)[0]

    def _find_ipv4_start(self) -> int:
        if self.ip_version != 6:
            return 0
        node = 0
        for _ in range(96):
            if node >= self.node_count:
                break
            node = self._record(node, 0)
        return node

    def lookup(self, ip: str) -> dict | None:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return None
        if addr.version == 6 and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        if addr.version == 6 and self.ip_version == 4:
            return None
        packed = addr.packed
        node = self._ipv4_start if addr.version == 4 else 0
        bits = len(packed) * 8
        for i in range(bits):
            if node >= self.node_count:
                break
            bit = (packed[i >> 3] >> (7 - (i & 7))) & 1
            node = self._record(node, bit)
        if node == self.node_count:
            return None
        if node < self.node_count:
            raise InvalidDatabase(f"{self.path}: search tree ended inside the tree")
        offset = (node - self.node_count) - 16
        value, _ = self._decode(self.data_start + offset, self.data_start)
        return value if isinstance(value, dict) else None

    # -- data section

    def _decode(self, off: int, base: int):
        b = self.buf
        ctrl = b[off]
        off += 1
        kind = ctrl >> 5
        if kind == 1:  # pointer: its own size encoding, then the value it points at
            ss, vvv = (ctrl >> 3) & 0x3, ctrl & 0x7
            if ss == 0:
                ptr, off = (vvv << 8) | b[off], off + 1
            elif ss == 1:
                ptr, off = ((vvv << 16) | (b[off] << 8) | b[off + 1]) + 2048, off + 2
            elif ss == 2:
                ptr, off = ((vvv << 24) | (b[off] << 16) | (b[off + 1] << 8) | b[off + 2]) + 526336, off + 3
            else:
                ptr, off = struct.unpack_from(">I", b, off)[0], off + 4
            value, _ = self._decode(base + ptr, base)
            return value, off
        if kind == 0:
            kind = 7 + b[off]
            off += 1
        size = ctrl & 0x1F
        if size >= 29:
            if size == 29:
                size, off = 29 + b[off], off + 1
            elif size == 30:
                size, off = 285 + ((b[off] << 8) | b[off + 1]), off + 2
            else:
                size, off = 65821 + ((b[off] << 16) | (b[off + 1] << 8) | b[off + 2]), off + 3
        if kind == 2:
            return bytes(b[off:off + size]).decode("utf-8", "replace"), off + size
        if kind == 3:
            return struct.unpack_from(">d", b, off)[0], off + 8
        if kind == 4:
            return bytes(b[off:off + size]), off + size
        if kind in (5, 6, 9, 10):
            return int.from_bytes(b[off:off + size], "big"), off + size
        if kind == 7:
            out = {}
            for _ in range(size):
                key, off = self._decode(off, base)
                out[key], off = self._decode(off, base)
            return out, off
        if kind == 8:
            raw = int.from_bytes(b[off:off + size], "big")
            if size == 4 and raw & 0x80000000:
                raw -= 1 << 32
            return raw, off + size
        if kind == 11:
            items = []
            for _ in range(size):
                item, off = self._decode(off, base)
                items.append(item)
            return items, off
        if kind == 14:
            return bool(size), off
        if kind == 15:
            return struct.unpack_from(">f", b, off)[0], off + 4
        if kind in (12, 13):
            return None, off
        raise InvalidDatabase(f"{self.path}: unknown type {kind} at {off - 1}")


def normalise(record: dict | None) -> Geo:
    """IPinfo Lite and the GeoLite2 schemas, as one shape."""
    if not record:
        return Geo()
    country = record.get("country_code") or ""
    if not country and isinstance(record.get("country"), dict):
        country = record["country"].get("iso_code", "")
    asn = record.get("asn") or record.get("autonomous_system_number") or ""
    asn = str(asn).upper().removeprefix("AS")
    network = record.get("as_name") or record.get("autonomous_system_organization") or ""
    city = region = tz = ""
    if isinstance(record.get("city"), dict):
        city = (record["city"].get("names") or {}).get("en", "")
    subdivisions = record.get("subdivisions")
    if isinstance(subdivisions, list) and subdivisions and isinstance(subdivisions[0], dict):
        region = (subdivisions[0].get("names") or {}).get("en", "") or subdivisions[0].get("iso_code", "")
    if isinstance(record.get("location"), dict):
        tz = record["location"].get("time_zone") or ""
    return Geo(str(country)[:8], asn[:12], str(network)[:120], str(city)[:80], str(region)[:80], str(tz)[:64])


class Geolocator:
    """Every database it was given, asked in order, answers merged."""

    def __init__(self, paths: list[str]):
        self.readers = []
        for path in paths:
            try:
                self.readers.append(Reader(path))
            except (OSError, InvalidDatabase) as e:
                print(f"geo: skipping {path}: {e}")

    def __bool__(self) -> bool:
        return bool(self.readers)

    @lru_cache(maxsize=4096)
    def lookup(self, ip: str | None) -> Geo:
        if not ip:
            return Geo()
        out = Geo()
        for reader in self.readers:
            try:
                out = out.merge(normalise(reader.lookup(ip)))
            except (InvalidDatabase, IndexError, struct.error):
                continue
        return out

    @classmethod
    def from_env(cls) -> "Geolocator":
        """`GEO_MMDB` is a comma-separated list of files. Missing ones are skipped, so a
        build without the database still runs, just without lookups."""
        paths = [p.strip() for p in os.environ.get("GEO_MMDB", "").split(",") if p.strip()]
        return cls([p for p in paths if os.path.exists(p)])
