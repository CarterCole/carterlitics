"""Sign-in for the console: OpenID Connect (authorization code + PKCE) against one
issuer, accounts.cartercole.net by default. Only administrators get a session: an
account with the configured role (the hub's `god`), or whose `sub` is listed.

The session is an HMAC-signed cookie scoped to the mount path (`/_c/`), so a site's
own pages never see it.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

from .web import NO_STORE, Request, Response, cookie, redirect

SESSION_COOKIE = "cl_session"
FLOW_COOKIE = "cl_flow"
SESSION_SECONDS = 7 * 86400
FLOW_SECONDS = 10 * 60
USER_AGENT = "carterlitics"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(secret: str, purpose: str, data: dict) -> str:
    """`payload.mac`, HMAC-SHA256 over the purpose too, so a flow cookie can't pass as a session."""
    if not secret:
        raise RuntimeError("SESSION_SECRET is not set")
    payload = b64url(json.dumps(data, separators=(",", ":")).encode())
    mac = hmac.new(secret.encode(), f"{purpose}.{payload}".encode(), hashlib.sha256).digest()
    return f"{payload}.{b64url(mac)}"


def unsign(secret: str, purpose: str, value: str | None) -> dict | None:
    if not secret or not value or value.count(".") != 1:
        return None
    payload, mac = value.split(".")
    want = hmac.new(secret.encode(), f"{purpose}.{payload}".encode(), hashlib.sha256).digest()
    try:
        if not hmac.compare_digest(unb64url(mac), want):
            return None
        data = json.loads(unb64url(payload))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("exp"), int) or data["exp"] < time.time():
        return None
    return data


def server_key(secret: str, site: str) -> str:
    """The bearer key a site's backend uses for /_c/server/*. Derived, so there is one
    secret to keep however many sites there are."""
    return hmac.new(secret.encode(), f"server-key:{site}".encode(), hashlib.sha256).hexdigest()[:40]


def page(status: int, title: str, text: str, cookies: list[str] | None = None, home: str = "/") -> Response:
    """Sign-in errors. Static text only, never input."""
    body = (
        "<!doctype html><html lang=en><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width, initial-scale=1'>"
        f"<meta name=robots content=noindex><title>{title} · carterlitics</title>"
        "<body style='margin:0;min-height:100vh;display:grid;place-items:center;background:#0e1116;"
        "color:#d6dde6;font:15px/1.6 system-ui,sans-serif;padding:16px'>"
        f"<main style='max-width:34rem'><p style='color:#f2777a;font-weight:600'>{title}</p>"
        f"<p>{text}</p><p><a style='color:#7cc4fa' href='{home}'>&larr; Back</a></p></main>"
    )
    return Response(status, body, {"Content-Type": "text/html; charset=utf-8", **NO_STORE}, cookies or [])


# ---------------------------------------------------------------- the issuer


_discovery: dict[str, dict] = {}


def http_json(url: str, *, form: dict | None = None, basic: tuple[str, str] | None = None,
              bearer: str | None = None) -> dict:
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if basic:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{basic[0]}:{basic[1]}".encode()).decode()
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=8) as res:
            return json.loads(res.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{url}: {e.code} {e.read()[:300]!r}") from None


def endpoints(issuer: str, fetch=http_json) -> dict:
    """The issuer's endpoints from its discovery document, cached per cold start. Falls
    back to Kith's paths if discovery is unreachable."""
    if issuer not in _discovery:
        try:
            doc = fetch(f"{issuer}/.well-known/openid-configuration")
        except (RuntimeError, OSError, ValueError):
            doc = {}
        _discovery[issuer] = {
            "authorize": doc.get("authorization_endpoint") or f"{issuer}/oauth/authorize",
            "token": doc.get("token_endpoint") or f"{issuer}/oauth/token",
            "userinfo": doc.get("userinfo_endpoint") or f"{issuer}/oauth/userinfo",
            "logout": doc.get("end_session_endpoint") or f"{issuer}/oauth/logout",
        }
    return _discovery[issuer]


def jwt_claims(token: str) -> dict:
    """The payload of a JWT, unverified. Only for the ID token in `callback`, see there."""
    try:
        claims = json.loads(unb64url(token.split(".")[1]))
    except (IndexError, ValueError):
        raise RuntimeError("malformed id_token") from None
    if not isinstance(claims, dict):
        raise RuntimeError("malformed id_token")
    return claims


# ---------------------------------------------------------------- flow


def safe_next(cfg, value: str | None) -> str:
    """A path under the console to come back to, so `?next=` can't send anyone off-site."""
    home = f"{cfg.mount}/admin/"
    if not value or not value.startswith(cfg.mount + "/") or value.startswith("//") or "\\" in value:
        return home
    parts = urllib.parse.urlsplit(value)
    return home if parts.scheme or parts.netloc else value


def _cookie(cfg, name: str, value: str, age: int) -> str:
    return cookie(name, value, age, path=cfg.mount + "/", secure=cfg.secure)


def login(req: Request, cfg) -> Response:
    if cfg.dev:
        # Local only: no issuer to talk to, so you are the administrator.
        session = sign(cfg.session_secret, "session", {"sub": "local-admin", "roles": [cfg.admin_role],
                                                       "name": "local admin", "exp": int(time.time()) + 86400})
        return redirect(safe_next(cfg, req.query.get("next")), [_cookie(cfg, SESSION_COOKIE, session, 86400)])
    if not cfg.issuer or not cfg.client_id or not cfg.session_secret:
        return page(503, "Sign-in is off", "No OpenID Connect client is configured on this deployment.")
    state, nonce, verifier = secrets.token_urlsafe(16), secrets.token_urlsafe(16), secrets.token_urlsafe(32)
    flow = sign(cfg.session_secret, "flow", {"state": state, "nonce": nonce, "verifier": verifier,
                                             "next": safe_next(cfg, req.query.get("next")),
                                             "exp": int(time.time()) + FLOW_SECONDS})
    url = endpoints(cfg.issuer)["authorize"] + "?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": cfg.client_id, "redirect_uri": cfg.redirect_uri,
        "scope": "openid profile groups", "state": state, "nonce": nonce,
        "code_challenge": b64url(hashlib.sha256(verifier.encode()).digest()),
        "code_challenge_method": "S256",
    })
    return redirect(url, [_cookie(cfg, FLOW_COOKIE, flow, FLOW_SECONDS)])


def callback(req: Request, cfg, fetch=http_json) -> Response:
    flow = unsign(cfg.session_secret, "flow", req.cookies.get(FLOW_COOKIE))
    clear = [_cookie(cfg, FLOW_COOKIE, "", 0)]
    home = f"{cfg.mount}/admin/"
    q = req.query
    if q.get("error"):
        return page(400, "Sign-in cancelled", "The account service did not sign you in.", clear, home)
    if not flow or not q.get("state") or not hmac.compare_digest(q["state"], flow["state"]):
        return page(400, "Sign-in expired", "That sign-in link is stale. Start again from the console.", clear, home)
    if q.get("iss") and q["iss"] != cfg.issuer:  # RFC 9207
        return page(400, "Sign-in refused", "The reply came from the wrong issuer.", clear, home)
    try:
        urls = endpoints(cfg.issuer, fetch)
        tokens = fetch(urls["token"], form={
            "grant_type": "authorization_code", "code": q.get("code", ""),
            "redirect_uri": cfg.redirect_uri, "code_verifier": flow["verifier"],
        }, basic=(cfg.client_id, cfg.client_secret))
        # Straight from the token endpoint over TLS, with the client secret: OIDC Core
        # 3.1.3.7 lets TLS stand in for the signature check here. The claims are still checked.
        claims = jwt_claims(tokens.get("id_token", ""))
        aud = claims.get("aud")
        if claims.get("iss") != cfg.issuer or cfg.client_id not in (aud if isinstance(aud, list) else [aud]) \
                or not isinstance(claims.get("exp"), (int, float)) or claims["exp"] < time.time() \
                or claims.get("nonce") != flow["nonce"] or not isinstance(claims.get("sub"), str):
            return page(400, "Sign-in refused", "The account service's reply did not check out.", clear, home)
        roles = claims.get("roles")
        if roles is None and tokens.get("access_token"):
            roles = fetch(urls["userinfo"], bearer=tokens["access_token"]).get("roles")
    except (RuntimeError, OSError, ValueError, AttributeError):
        traceback.print_exc()
        return page(502, "Sign-in failed", "The account service could not be reached. Try again.", clear, home)
    roles = [r for r in roles if isinstance(r, str)] if isinstance(roles, list) else []
    sub = claims["sub"]
    if not cfg.is_admin(sub, roles):
        print(json.dumps({"event": "admin_denied", "sub": sub}))
        return page(403, "Access denied", "That account is not an administrator here.", clear, home)
    name = claims.get("name") or claims.get("preferred_username") or ""
    session = sign(cfg.session_secret, "session", {"sub": sub, "roles": roles, "name": str(name)[:80],
                                                   "exp": int(time.time()) + SESSION_SECONDS})
    return redirect(flow["next"], clear + [_cookie(cfg, SESSION_COOKIE, session, SESSION_SECONDS)])


def session_user(req: Request, cfg) -> dict | None:
    s = unsign(cfg.session_secret, "session", req.cookies.get(SESSION_COOKIE))
    # Checked again on every request, so taking someone off ADMIN_IDS takes effect at once.
    if not s or not cfg.is_admin(s.get("sub", ""), s.get("roles")):
        return None
    return {"id": s["sub"], "name": s.get("name") or "", "roles": s.get("roles") or []}


def same_origin(req: Request, cfg) -> bool:
    origin = req.headers.get("origin", "")
    console = cfg.console_url().rsplit(cfg.mount, 1)[0]
    return origin == console


def logout(req: Request, cfg) -> Response:
    if not same_origin(req, cfg):
        return page(403, "Refused", "Sign out from the console.")
    clear = [_cookie(cfg, SESSION_COOKIE, "", 0)]
    home = cfg.console_url().rsplit(cfg.mount, 1)[0] + "/"
    if cfg.dev or not cfg.client_id:
        return redirect(home, clear, 303)
    url = endpoints(cfg.issuer)["logout"] + "?" + urllib.parse.urlencode({
        "client_id": cfg.client_id, "post_logout_redirect_uri": home})
    return redirect(url, clear, 303)
