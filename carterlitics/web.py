"""A tiny HTTP layer: Request, Response, cookies, and Lambda's Function URL events."""
from __future__ import annotations

import base64
import json
import urllib.parse
from dataclasses import dataclass, field

NO_STORE = {"Cache-Control": "no-store"}


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)  # lowercase names
    cookies: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    def json(self):
        # sendBeacon posts text/plain to stay a simple request, so the type is not checked.
        try:
            return json.loads(self.body or b"null")
        except ValueError:
            return None

    @property
    def ip(self) -> str | None:
        # Set by CloudFront from the connection itself, so the client cannot forge it:
        # "198.51.100.7:46532", or an IPv6 address with ":port" on the end.
        addr = self.headers.get("cloudfront-viewer-address")
        if addr and ":" in addr:
            return addr.rsplit(":", 1)[0].strip("[]")
        # Each hop appends its peer; the second from the end is what CloudFront saw.
        hops = [h.strip() for h in self.headers.get("x-forwarded-for", "").split(",") if h.strip()]
        return hops[-2] if len(hops) >= 2 else (hops[0] if hops else None)

    @property
    def opted_out(self) -> bool:
        return self.headers.get("sec-gpc") == "1" or self.headers.get("dnt") == "1"


@dataclass
class Response:
    status: int
    body: str | bytes = ""
    headers: dict[str, str] = field(default_factory=dict)
    cookies: list[str] = field(default_factory=list)


def reply_json(data, status: int = 200, headers: dict | None = None) -> Response:
    return Response(status, json.dumps(data, separators=(",", ":"), default=str),
                    {"Content-Type": "application/json; charset=utf-8", **NO_STORE, **(headers or {})})


def no_content() -> Response:
    return Response(204, "", dict(NO_STORE))


def plain(status: int, text: str) -> Response:
    return Response(status, text, {"Content-Type": "text/plain; charset=utf-8", **NO_STORE})


def redirect(location: str, cookies: list[str] | None = None, status: int = 302) -> Response:
    return Response(status, "", {"Location": location, **NO_STORE}, cookies or [])


def cookie(name: str, value: str, max_age: int, path: str = "/", secure: bool = True,
           http_only: bool = True) -> str:
    parts = [f"{name}={value}", f"Path={path}", f"Max-Age={max_age}", "SameSite=Lax"]
    if secure:
        parts.append("Secure")
    if http_only:
        parts.append("HttpOnly")
    return "; ".join(parts)


def from_lambda(event: dict) -> Request:
    """A Function URL event (payload format 2.0)."""
    body = event.get("body") or ""
    raw = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode()
    cookies = {}
    for c in event.get("cookies") or []:
        name, _, value = c.partition("=")
        cookies.setdefault(name.strip(), value.strip())
    return Request(
        method=event.get("requestContext", {}).get("http", {}).get("method", "GET"),
        path=event.get("rawPath", "/"),
        query=dict(urllib.parse.parse_qsl(event.get("rawQueryString", ""))),
        headers={k.lower(): v for k, v in (event.get("headers") or {}).items()},
        cookies=cookies,
        body=raw,
    )


def to_lambda(res: Response) -> dict:
    out = {"statusCode": res.status, "headers": res.headers, "cookies": res.cookies}
    if isinstance(res.body, bytes):
        return {**out, "body": base64.b64encode(res.body).decode(), "isBase64Encoded": True}
    return {**out, "body": res.body}
