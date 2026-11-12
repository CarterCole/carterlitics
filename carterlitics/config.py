"""Which sites this collector serves, how long it keeps things, and its secrets.

The site list and retention come from a TOML file (`carterlitics.toml`) locally, and
from the `CARTERLITICS_CONFIG` environment variable (the same data as JSON) on Lambda,
where the deploy script puts it. Secrets only ever come from the environment.
"""
from __future__ import annotations

import json
import os
import re
import tomllib
import urllib.parse
from dataclasses import dataclass, field

RETENTION = {          # days
    "counts": 400,     # daily counters
    "visits": 400,     # consented page views: IP, user agent, visitor ID
    "events": 180,
    "errors": 90,
    "server": 30,      # CloudFront requests, people and bots
    "recordings": 30,  # session recordings and the session list
}


MAX_STEPS = 10
FUNNEL_BY = ("session", "visitor")
EVENT_NAME = re.compile(r"[A-Za-z0-9_.:\- *]{1,64}")


@dataclass(frozen=True)
class Step:
    """One funnel step, written as a string:

        /pricing                  a page view of that page (`/pricing/`, `/pricing.html` too)
        /blog/*                   any page under /blog/ (`*` matches anything, `/` included)
        event:sign_up             an event of that name (`*` works here too)
        event:click?target=buy    ... whose properties have these values (`&` for more)
    """
    text: str
    kind: str                                  # "page" or "event"
    pattern: re.Pattern
    props: tuple[tuple[str, str], ...] = ()

    @classmethod
    def parse(cls, text) -> "Step":
        if not isinstance(text, str) or not text.strip():
            raise ValueError("a funnel step is a page path (/pricing) or an event (event:sign_up)")
        text = text.strip()
        if text.startswith("event:"):
            name, _, query = text[len("event:"):].partition("?")
            if not EVENT_NAME.fullmatch(name):
                raise ValueError(f"funnel step {text!r}: an event name is letters, digits, _ . : - and spaces")
            props = tuple(urllib.parse.parse_qsl(query, keep_blank_values=True))
            return cls(text, "event", _glob(name), props)
        if text.startswith("/") and "?" not in text and "#" not in text and len(text) <= 200:
            path = text.rstrip("/") or "/"
            if path.endswith("/index.html"):
                path = path[: -len("index.html")].rstrip("/") or "/"
            elif path.endswith(".html"):
                path = path[:-5]
            return cls(text, "page", _glob(path))
        raise ValueError(f"funnel step {text!r}: start it with / for a page or event: for an event")

    def matches(self, kind: str, value: str, props: dict | None = None) -> bool:
        if kind != self.kind or not self.pattern.fullmatch(value or ""):
            return False
        props = props or {}
        return all(k in props and _prop_text(props[k]) == v for k, v in self.props)


def _glob(text: str) -> re.Pattern:
    return re.compile(".*".join(re.escape(part) for part in text.split("*")))


def _prop_text(value) -> str:
    """A property as a step's `key=value` compares it: the string form JSON would give."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


@dataclass(frozen=True)
class Funnel:
    """Steps a visitor takes in order, counted over consented sessions (or visitors)."""
    name: str
    steps: tuple[Step, ...]
    by: str = "session"

    @classmethod
    def parse(cls, data: dict, where: str = "") -> "Funnel":
        if not isinstance(data, dict):
            raise ValueError(f"{where}: a funnel is a table with name and steps")
        name = str(data.get("name") or "").strip()
        steps = data.get("steps")
        if not name or len(name) > 80:
            raise ValueError(f"{where}: a funnel needs a name (up to 80 characters)")
        about = f"{where} funnel {name!r}: " if where else ""   # none for one typed in the console
        if not isinstance(steps, list) or not 2 <= len(steps) <= MAX_STEPS:
            raise ValueError(f"{about}give 2 to {MAX_STEPS} steps, in a list")
        by = data.get("by", "session")
        if by not in FUNNEL_BY:
            raise ValueError(f"{about}by is 'session' or 'visitor'")
        try:
            parsed = tuple(Step.parse(s) for s in steps)
        except ValueError as e:
            raise ValueError(f"{about}{e}") from None
        return cls(name, parsed, by)

    def to_data(self) -> dict:
        return {"name": self.name, "by": self.by, "steps": [s.text for s in self.steps]}


def google_account(google: dict) -> str:
    """The service account's address: given whole (`account`), or made from `project` and
    `service_account` (its name, "carterlitics" unless set)."""
    if google.get("account"):
        return str(google["account"])
    if google.get("project"):
        return f"{google.get('service_account', 'carterlitics')}@{google['project']}.iam.gserviceaccount.com"
    return ""


@dataclass(frozen=True)
class Site:
    id: str                          # "cartercole.net": the key everything is stored under
    url: str                         # "https://cartercole.net"
    aliases: tuple[str, ...] = ()    # other hosts that serve it: "www.cartercole.net", or "*.cartercole.net"
    exclude: tuple[str, ...] = ()    # path prefixes that are never pages: "/admin"
    distribution: str = ""           # its CloudFront distribution; found by alias if empty (deploy only)
    server_ip: str = "full"          # "full" or "truncate": IPs in the server request log
    dev_url: str = ""                # its local dev server, for links and the CSP
    client: bool = True              # its pages load c.js; false = server logs only
    funnels: tuple[Funnel, ...] = () # [[site.funnel]]: the console's Funnels tab
    consent_docs: str = ""           # "/privacy/v{version}": each consent version's policy, for the console

    @property
    def consent_docs_url(self) -> str:
        """`consent_docs` on the site: "https://cartercole.com/privacy/v{version}", or ""."""
        return self.url.rstrip("/") + self.consent_docs if self.consent_docs else ""

    @property
    def host(self) -> str:
        return urllib.parse.urlsplit(self.url).hostname or self.id

    @property
    def hosts(self) -> frozenset[str]:
        """Every host name it answers to, without `www.`: what isn't a referral."""
        names = {self.id, self.host, *self.aliases}
        if self.dev_url:
            names.add(urllib.parse.urlsplit(self.dev_url).hostname or "")
        return frozenset(n.lower().removeprefix("www.") for n in names if n)

    @property
    def origins(self) -> list[str]:
        out = [self.url.rstrip("/")]
        scheme = urllib.parse.urlsplit(self.url).scheme
        out += [f"{scheme}://{a}" for a in self.aliases]
        if self.dev_url:
            out.append(self.dev_url.rstrip("/"))
        return out


@dataclass(frozen=True)
class Config:
    sites: dict[str, Site] = field(default_factory=dict)
    console: str = ""                 # the site whose /_c/admin is the console; others redirect
    mount: str = "/_c"
    retention: dict[str, int] = field(default_factory=lambda: dict(RETENTION))
    # sign-in (OpenID Connect)
    issuer: str = ""
    client_id: str = ""
    client_secret: str = ""
    admin_role: str = "god"
    admin_ids: frozenset[str] = frozenset()
    # Search Console: the service account that reads it ([google] in carterlitics.toml)
    google_account: str = ""
    # secrets
    origin_secret: str = ""           # CloudFront sends it; requests without it are refused
    session_secret: str = ""          # signs the console's session cookie
    server_secret: str = ""           # derives each site's server key
    dev: bool = False                 # local: no CloudFront, sign-in is a shortcut

    # -- lookups

    def site_for_host(self, host: str | None) -> Site | None:
        """The site a host belongs to: an exact name first, then a `*.domain` alias."""
        host = (host or "").lower().split(":")[0].removeprefix("www.")
        for site in self.sites.values():
            if not site.id.startswith("*.") and host in site.hosts:
                return site
        for site in self.sites.values():
            if any(h.startswith("*.") and host.endswith(h[1:]) for h in site.hosts):
                return site
        return None

    @property
    def console_site(self) -> Site | None:
        return self.sites.get(self.console) or next(iter(self.sites.values()), None)

    def console_url(self) -> str:
        site = self.console_site
        if not site:
            return ""
        base = site.dev_url if self.dev and site.dev_url else site.url
        return base.rstrip("/") + self.mount

    @property
    def redirect_uri(self) -> str:
        return f"{self.console_url()}/auth/callback"

    def is_admin(self, sub: str, roles) -> bool:
        return sub in self.admin_ids or (bool(self.admin_role) and self.admin_role in (roles or []))

    @property
    def secure(self) -> bool:
        return self.console_url().startswith("https://")

    # -- loading

    @classmethod
    def from_data(cls, data: dict, **overrides) -> "Config":
        sites = {}
        for s in data.get("site", []):
            site = Site(
                id=s["id"], url=s.get("url") or f"https://{s['id']}",
                aliases=tuple(s.get("aliases", ())), exclude=tuple(s.get("exclude", ())),
                distribution=s.get("distribution", ""), server_ip=s.get("server_ip", "full"),
                dev_url=s.get("dev_url", ""), client=bool(s.get("client", True)),
                funnels=tuple(Funnel.parse(f, s["id"]) for f in s.get("funnel", ())),
                consent_docs=s.get("consent_docs", ""),
            )
            if site.server_ip not in ("full", "truncate"):
                raise ValueError(f"{site.id}: server_ip must be 'full' or 'truncate'")
            if site.consent_docs and not (site.consent_docs.startswith("/") and "{version}" in site.consent_docs):
                raise ValueError(f"{site.id}: consent_docs is a path with {{version}} in it, like /privacy/v{{version}}")
            names = [f.name for f in site.funnels]
            if len(names) != len(set(names)):
                raise ValueError(f"{site.id}: two funnels share a name")
            sites[site.id] = site
        retention = {**RETENTION, **{k: int(v) for k, v in data.get("retention", {}).items()}}
        unknown = set(retention) - set(RETENTION)
        if unknown:
            raise ValueError(f"unknown retention keys: {', '.join(sorted(unknown))}")
        auth = data.get("auth", {})
        return cls(
            sites=sites, console=data.get("console", ""), mount=data.get("mount", "/_c").rstrip("/"),
            retention=retention, issuer=auth.get("issuer", "").rstrip("/"),
            admin_role=auth.get("admin_role", "god"),
            admin_ids=frozenset(auth.get("admin_ids", ())),
            google_account=google_account(data.get("google", {})), **overrides,
        )

    @classmethod
    def load(cls, path: str, **overrides) -> "Config":
        with open(path, "rb") as f:
            return cls.from_data(tomllib.load(f), **overrides)

    @classmethod
    def from_env(cls) -> "Config":
        env = os.environ
        data = json.loads(env.get("CARTERLITICS_CONFIG") or "{}")
        ids = {i.strip() for i in env.get("ADMIN_IDS", "").split(",") if i.strip()}
        cfg = cls.from_data(data, client_id=env.get("OIDC_CLIENT_ID", ""),
                            client_secret=env.get("OIDC_CLIENT_SECRET", ""),
                            origin_secret=env.get("ORIGIN_SECRET", ""),
                            session_secret=env.get("SESSION_SECRET", ""),
                            server_secret=env.get("SERVER_SECRET", ""))
        if ids:
            cfg = cls(**{**cfg.__dict__, "admin_ids": cfg.admin_ids | ids})
        return cfg

    def to_data(self) -> dict:
        """The non-secret part, as the deploy script hands it to Lambda."""
        return {
            "console": self.console, "mount": self.mount, "retention": self.retention,
            "auth": {"issuer": self.issuer, "admin_role": self.admin_role,
                     "admin_ids": sorted(self.admin_ids)},
            **({"google": {"account": self.google_account}} if self.google_account else {}),
            "site": [{"id": s.id, "url": s.url, "aliases": list(s.aliases), "exclude": list(s.exclude),
                      "server_ip": s.server_ip, **({"consent_docs": s.consent_docs} if s.consent_docs else {}),
                      **({"funnel": [f.to_data() for f in s.funnels]} if s.funnels else {})}
                     for s in self.sites.values()],
        }
