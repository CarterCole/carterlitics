"""CloudFront access logs -> LogLine records.

CloudFront writes them to the bucket (standard logging v2, JSON or W3C; or legacy
standard logging, W3C), the bucket tells the Lambda, and the Lambda reads each file.
This is the server side of the analytics: it sees every request, including the ones
CloudFront answered from cache and the bots that never run JavaScript.

Both formats are read by field name, so the field selection and order don't matter.
"""
from __future__ import annotations

import gzip
import json
import urllib.parse
from datetime import datetime, timezone
from typing import Iterable, Iterator

from .store import LogLine


def _unquote(value: str) -> str:
    """CloudFront percent-encodes these fields, sometimes twice."""
    if not value or value == "-":
        return ""
    once = urllib.parse.unquote(value)
    return urllib.parse.unquote(once) if "%2" in once or "%3" in once else once


def _num(value, kind=int):
    try:
        return kind(value)
    except (TypeError, ValueError):
        return kind(0)


def _record(f: dict) -> LogLine | None:
    """One log entry as a dict keyed by CloudFront's field names."""
    at = None
    for key in ("timestamp(ms)", "timestamp"):
        if f.get(key) not in (None, "", "-"):
            seconds = _num(f[key], float) / (1000 if key == "timestamp(ms)" else 1)
            at = datetime.fromtimestamp(seconds, timezone.utc)
            break
    if at is None and f.get("date") and f.get("time"):
        try:
            at = datetime.strptime(f"{f['date']} {f['time']}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    if at is None:
        return None
    host = f.get("x-host-header") or f.get("cs(Host)") or ""
    ip = f.get("c-ip") or ""
    if not host or host == "-" or not ip or ip == "-":
        return None
    return LogLine(
        at=at, host=str(host).lower(), ip=str(ip), method=str(f.get("cs-method") or "GET"),
        path=_unquote(str(f.get("cs-uri-stem") or "/")) or "/",
        query=_unquote(str(f.get("cs-uri-query") or "")),
        status=_num(f.get("sc-status")), referrer=_unquote(str(f.get("cs(Referer)") or "")),
        user_agent=_unquote(str(f.get("cs(User-Agent)") or "")), bytes=_num(f.get("sc-bytes")),
        edge=str(f.get("x-edge-result-type") or "").replace("-", ""),
        seconds=_num(f.get("time-taken"), float),
        country=str(f.get("c-country") or f.get("country") or "").replace("-", ""),
        asn=str(f.get("asn") or f.get("c-asn") or "").replace("-", ""),
    )


def parse(data: bytes) -> Iterator[LogLine]:
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    yield from parse_lines(data.decode("utf-8", "replace").splitlines())


def parse_lines(lines: Iterable[str]) -> Iterator[LogLine]:
    fields: list[str] = []
    for raw in lines:
        line = raw.rstrip("\r\n")
        if not line:
            continue
        if line.startswith("#Fields:"):
            fields = line.split(":", 1)[1].split()
            continue
        if line.startswith("#"):
            continue
        if line.startswith("{"):
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            record = _record(entry) if isinstance(entry, dict) else None
        elif fields:
            record = _record(dict(zip(fields, line.split("\t"))))
        else:
            record = None
        if record:
            yield record


def s3_objects(event: dict) -> list[tuple[str, str]]:
    """(bucket, key) for each object an S3 notification names."""
    out = []
    for r in event.get("Records") or []:
        s3 = r.get("s3") or {}
        bucket = (s3.get("bucket") or {}).get("name")
        key = (s3.get("object") or {}).get("key")
        if bucket and key and str(r.get("eventName", "ObjectCreated")).startswith("ObjectCreated"):
            out.append((bucket, urllib.parse.unquote_plus(key)))
    return out
