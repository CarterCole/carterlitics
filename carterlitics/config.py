"""Which sites this collector serves, how long it keeps things, and its secrets.

The site list and retention come from a TOML file (`carterlitics.toml`) locally, and
from the `CARTERLITICS_CONFIG` environment variable (the same data as JSON) on Lambda,
where the deploy script puts it. Secrets only ever come from the environment.
"""
from __future__ import annotations

import json
import os
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
            )
            if site.server_ip not in ("full", "truncate"):
                raise ValueError(f"{site.id}: server_ip must be 'full' or 'truncate'")
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
            admin_ids=frozenset(auth.get("admin_ids", ())), **overrides,
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
            "site": [{"id": s.id, "url": s.url, "aliases": list(s.aliases), "exclude": list(s.exclude),
                      "server_ip": s.server_ip} for s in self.sites.values()],
        }
