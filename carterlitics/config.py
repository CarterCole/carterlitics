"""Which sites this collector serves, how long it keeps things, and its secrets.

The site list and retention come from a TOML file (`carterlitics.toml`) locally, and
from the `CARTERLITICS_CONFIG` environment variable (the same data as JSON) on Lambda,
where the deploy script puts it. Experiments, with their CSS and copy, are too big for
Lambda's 4 KB of environment: `infra/deploy.py` writes them into the code package as
`experiments.json` instead. Secrets only ever come from the environment.
"""
from __future__ import annotations

import dataclasses
import itertools
import json
import os
import re
import tomllib
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

PACKAGED = Path(__file__).with_name("experiments.json")   # written into the code package by infra/deploy.py

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
FORM_NAME = re.compile(r"[A-Za-z0-9_.:\-\[\]/#@ *]{1,64}")   # a form's key (store.FORM_KEY), and *


@dataclass(frozen=True)
class Step:
    """One funnel step, written as a string:

        /pricing                  a page view of that page (`/pricing/`, `/pricing.html` too)
        /blog/*                   any page under /blog/ (`*` matches anything, `/` included)
        event:sign_up             an event of that name (`*` works here too)
        event:click?target=buy    ... whose properties have these values (`&` for more)
        form:/subscribe           an attempt at the form of that key (store.py's form log), begun
        form:/subscribe?res=submit    ... sent; res=abandon: given up, last=email: in that field
    """
    text: str
    kind: str                                  # "page", "event" or "form"
    pattern: re.Pattern
    props: tuple[tuple[str, str], ...] = ()
    name: str = ""                             # what `pattern` matches, as a glob: the path, event or form

    @classmethod
    def parse(cls, text) -> "Step":
        if not isinstance(text, str) or not text.strip():
            raise ValueError("a funnel step is a page path (/pricing), an event (event:sign_up) or a form (form:signup)")
        text = text.strip()
        if text.startswith("event:"):
            name, _, query = text[len("event:"):].partition("?")
            if not EVENT_NAME.fullmatch(name):
                raise ValueError(f"funnel step {text!r}: an event name is letters, digits, _ . : - and spaces")
            props = tuple(urllib.parse.parse_qsl(query, keep_blank_values=True))
            return cls(text, "event", _glob(name), props, name)
        if text.startswith("form:"):
            name, _, query = text[len("form:"):].partition("?")
            if not FORM_NAME.fullmatch(name) or name.strip() != name:
                raise ValueError(f"funnel step {text!r}: a form is named by its key, as the Forms tab shows it")
            props = tuple(urllib.parse.parse_qsl(query, keep_blank_values=True))
            return cls(text, "form", _glob(name), props, name)
        if text.startswith("/") and "?" not in text and "#" not in text and len(text) <= 200:
            path = text.rstrip("/") or "/"
            if path.endswith("/index.html"):
                path = path[: -len("index.html")].rstrip("/") or "/"
            elif path.endswith(".html"):
                path = path[:-5]
            return cls(text, "page", _glob(path), (), path)
        raise ValueError(f"funnel step {text!r}: start it with / for a page, event: for an event or form: for a form")

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


# ---------------------------------------------------------------- experiments

EXPERIMENT_KEY = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
VARIANT_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,23}")    # no "." or ":": markup is key.factor:variant
ATTR_NAME = re.compile(r"[A-Za-z_][-A-Za-z0-9_:.]{0,63}")
STATUSES = ("draft", "running", "paused", "done")
ALLOCATIONS = ("split", "thompson", "greedy")
AUDIENCES = ("everyone", "consented")
MAX_VARIANTS = 8
MAX_FACTORS = 4
MAX_ARMS = 16
MAX_METRICS = 6           # the goal and up to five more
MAX_CHANGES = 40          # text and attribute changes, a variant
MAX_CSS = 20_000
MAX_PAGES = 20


def _text(value, what: str, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{what} is a string of up to {limit} characters")
    return value


def _selector(value, what: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError(f"{what}: a CSS selector, up to 200 characters")
    return value.strip()


@dataclass(frozen=True)
class Variant:
    """One version of what an experiment varies: the original, or a change to it.

        css       a stylesheet, in force while the variant is shown
        text      {selector: text}: the matching elements' text, replaced
        attrs     {selector: {attribute: value}}: attributes set (not on* handlers, nor javascript: URLs)
        redirect  a path on the site to send the visitor to instead (a split-URL test)

    Markup in the page can carry variants too (data-cl-x), and so can the page's own code
    (cl("variant")); a variant then needs none of these."""
    id: str
    name: str = ""
    weight: float = 1.0
    css: str = ""
    text: tuple[tuple[str, str], ...] = ()
    attrs: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = ()
    redirect: str = ""

    @property
    def label(self) -> str:
        return self.name or self.id

    @property
    def changes(self) -> bool:
        return bool(self.css or self.text or self.attrs or self.redirect)

    @classmethod
    def parse(cls, data, where: str) -> "Variant":
        if not isinstance(data, dict):
            raise ValueError(f"{where}: a variant is a table with an id")
        vid = data.get("id")
        if not isinstance(vid, str) or not VARIANT_ID.fullmatch(vid):
            raise ValueError(f"{where}: a variant's id is lower-case letters, digits, - and _ (up to 24)")
        at = f"{where} variant {vid!r}"
        weight = data.get("weight", 1)
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not 0 < weight <= 1000:
            raise ValueError(f"{at}: weight is a number above 0")
        text = data.get("text") or {}
        attrs = data.get("attrs") or {}
        if not isinstance(text, dict) or not isinstance(attrs, dict):
            raise ValueError(f"{at}: text and attrs are tables keyed by CSS selector")
        texts = tuple((_selector(k, f"{at} text"), _text(v, f"{at} text for {k!r}", 2000)) for k, v in text.items())
        sets = []
        for sel, values in attrs.items():
            sel = _selector(sel, f"{at} attrs")
            if not isinstance(values, dict) or not values:
                raise ValueError(f"{at} attrs for {sel!r}: a table of attribute = value")
            pairs = []
            for name, value in values.items():
                if not ATTR_NAME.fullmatch(str(name)) or str(name).lower().startswith("on") or \
                        str(name).lower() == "srcdoc":
                    raise ValueError(f"{at} attrs for {sel!r}: {name!r} isn't an attribute it may set")
                value = _text(value, f"{at} attrs {sel!r} {name}", 2000)
                # A browser drops tabs, newlines and other controls anywhere in a URL's scheme:
                # `java\tscript:` runs as well as `javascript:`.
                if re.match(r"(javascript|vbscript|data):", re.sub(r"[\x00-\x20\x7f]", "", value), re.I):
                    raise ValueError(f"{at} attrs for {sel!r}: {name} may not be a script URL")
                pairs.append((str(name), value))
            sets.append((sel, tuple(pairs)))
        if len(texts) + len(sets) > MAX_CHANGES:
            raise ValueError(f"{at}: at most {MAX_CHANGES} text and attribute changes")
        redirect = _text(data.get("redirect"), f"{at} redirect", 300).strip()
        # A browser reads `/\evil.example` and `/<tab>/evil.example` as `//evil.example`: another site.
        if redirect and (not redirect.startswith("/") or redirect.startswith("//") or "#" in redirect or
                         "\\" in redirect or re.search(r"[\x00-\x20\x7f]", redirect)):
            raise ValueError(f"{at}: redirect is a path on the site, like /pricing-b")
        return cls(vid, _text(data.get("name"), f"{at} name", 80).strip(), float(weight),
                   _text(data.get("css"), f"{at} css", MAX_CSS), texts, tuple(sets), redirect)

    def to_data(self) -> dict:
        out: dict = {"id": self.id}
        if self.name:
            out["name"] = self.name
        if self.weight != 1:
            out["weight"] = self.weight
        if self.css:
            out["css"] = self.css
        if self.text:
            out["text"] = dict(self.text)
        if self.attrs:
            out["attrs"] = {sel: dict(pairs) for sel, pairs in self.attrs}
        if self.redirect:
            out["redirect"] = self.redirect
        return out


@dataclass(frozen=True)
class Factor:
    """A part of the page an experiment varies, and its variants: one, unnamed (id ""), in
    an A/B test; several in a multivariate test, every combination of them an arm."""
    id: str
    variants: tuple[Variant, ...]


@dataclass(frozen=True)
class Arm:
    """What a visitor in the experiment is shown: one variant of each factor."""
    id: str                         # "b", or in a multivariate test "a.blue": each factor's, in order
    name: str
    weight: float                   # its share under a split allocation, before normalising
    levels: tuple[Variant, ...]


@dataclass(frozen=True)
class Experiment:
    """A test on a site's pages: visitors split between versions of it, and which one does
    better at its goal. `[[site.experiment]]` in carterlitics.toml (README: Experiments)."""
    key: str
    name: str
    status: str                     # draft (preview only) · running · paused (the original) · done (the winner)
    factors: tuple[Factor, ...]
    goal: Step
    metrics: tuple[Step, ...] = ()  # more, reported but not optimised for
    pages: tuple[Step, ...] = ()    # where it runs; none: where its markup is, or the page asks
    traffic: float = 1.0            # the share of visitors in the test
    allocation: str = "split"       # split (by weight) · thompson · greedy (bandits, on the goal)
    explore: float = 0.1            # a bandit's share of traffic split evenly, whatever it has learned
    audience: str = "everyone"      # or consented
    winner: str = ""                # done: the arm everyone sees
    confidence: float = 0.95

    @property
    def multivariate(self) -> bool:
        return len(self.factors) > 1

    @property
    def arms(self) -> list[Arm]:
        """Every combination of the factors' variants, the first (each factor's first) the
        original. A split allocation weights an A/B test's by `weight`, a multivariate
        test's evenly."""
        out = []
        for combo in itertools.product(*(f.variants for f in self.factors)):
            if self.multivariate:   # each variant's name, or its factor and id: "One email · Bold", "head a · button blue"
                name = " · ".join(v.name or f"{f.id} {v.id}" for f, v in zip(self.factors, combo))
                weight = 1.0
            else:
                name, weight = combo[0].label, combo[0].weight
            out.append(Arm(".".join(v.id for v in combo), name, weight, combo))
        return out

    @property
    def all_metrics(self) -> tuple[Step, ...]:
        return (self.goal, *self.metrics)

    @property
    def changes(self) -> bool:
        return any(v.changes for f in self.factors for v in f.variants)

    @classmethod
    def parse(cls, data, where: str) -> "Experiment":
        if not isinstance(data, dict):
            raise ValueError(f"{where}: an experiment is a table with a key, a goal and variants")
        key = data.get("key")
        if not isinstance(key, str) or not EXPERIMENT_KEY.fullmatch(key):
            raise ValueError(f"{where}: an experiment's key is lower-case letters, digits, - and _ (up to 40)")
        at = f"{where} experiment {key!r}"
        status = data.get("status", "draft")
        if status not in STATUSES:
            raise ValueError(f"{at}: status is one of {', '.join(STATUSES)}")
        allocation = data.get("allocation", "split")
        if allocation not in ALLOCATIONS:
            raise ValueError(f"{at}: allocation is one of {', '.join(ALLOCATIONS)}")
        audience = data.get("audience", "everyone")
        if audience not in AUDIENCES:
            raise ValueError(f"{at}: audience is 'everyone' or 'consented'")

        def share(name: str, default: float, low: float, high: float, above: bool = False) -> float:
            value = data.get(name, default)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or \
                    not (low < value <= high if above else low <= value <= high):
                raise ValueError(f"{at}: {name} is a number from {low} to {high}")
            return float(value)

        traffic = share("traffic", 1.0, 0, 1, above=True)
        explore = share("explore", 0.1, 0, 1)
        confidence = share("confidence", 0.95, 0.8, 0.99)

        def steps(value, what: str) -> tuple[Step, ...]:
            try:
                return tuple(Step.parse(s) for s in value)
            except ValueError as e:
                raise ValueError(f"{at} {what}: {str(e).replace('funnel step', 'step')}") from None

        if not data.get("goal"):
            raise ValueError(f"{at}: give a goal, written as a funnel step (event:sign_up, /pricing, form:/subscribe)")
        goal, = steps([data["goal"]], "goal")
        metrics = data.get("metrics", [])
        if not isinstance(metrics, list) or len(metrics) > MAX_METRICS - 1:
            raise ValueError(f"{at}: metrics is a list of up to {MAX_METRICS - 1} more steps")
        metrics = tuple(m for m in steps(metrics, "metrics") if m.text != goal.text)
        if len({m.text for m in metrics}) != len(metrics):
            raise ValueError(f"{at}: a metric is listed twice")
        pages = data.get("pages", [])
        if not isinstance(pages, list) or len(pages) > MAX_PAGES:
            raise ValueError(f"{at}: pages is a list of up to {MAX_PAGES} paths")
        pages = steps(pages, "pages")
        if any(p.kind != "page" for p in pages):
            raise ValueError(f"{at}: pages are paths, like / or /blog/*")

        if "variant" in data and "factor" in data:
            raise ValueError(f"{at}: give variants (an A/B test) or factors (multivariate), not both")
        if "factor" in data:
            raw = data["factor"]
            if not isinstance(raw, list) or not 2 <= len(raw) <= MAX_FACTORS:
                raise ValueError(f"{at}: a multivariate test has 2 to {MAX_FACTORS} factors")
            factors = []
            for f in raw:
                fid = f.get("id") if isinstance(f, dict) else None
                if not isinstance(fid, str) or not VARIANT_ID.fullmatch(fid):
                    raise ValueError(f"{at}: a factor's id is lower-case letters, digits, - and _")
                factors.append(Factor(fid, cls._variants(f.get("variant"), f"{at} factor {fid!r}")))
            if len({f.id for f in factors}) != len(factors):
                raise ValueError(f"{at}: two factors share an id")
        else:
            factors = [Factor("", cls._variants(data.get("variant"), at))]
        exp = cls(key, _text(data.get("name"), f"{at} name", 80).strip(), status, tuple(factors), goal, metrics,
                  pages, traffic, allocation, explore, audience, str(data.get("winner") or ""), confidence)
        arms = exp.arms
        if len(arms) > MAX_ARMS:
            raise ValueError(f"{at}: {len(arms)} combinations; a test may have up to {MAX_ARMS}")
        if exp.winner and exp.winner not in {a.id for a in arms}:
            raise ValueError(f"{at}: winner {exp.winner!r} isn't one of its variants ({', '.join(a.id for a in arms)})")
        if status == "done" and not exp.winner:
            raise ValueError(f"{at}: a done experiment names its winner, the variant everyone now sees")
        redirects = [v for f in factors for v in f.variants if v.redirect]
        if redirects:
            if exp.multivariate:
                raise ValueError(f"{at}: a multivariate test can't redirect")
            if factors[0].variants[0].redirect:
                raise ValueError(f"{at}: the original (the first variant) stays where it is")
            if not pages:
                raise ValueError(f"{at}: a redirect needs pages, the ones it sends visitors away from")
            for v in redirects:
                target = v.redirect.split("?", 1)[0]
                target = (target.rstrip("/") or "/").removesuffix(".html")
                if any(p.matches("page", target) for p in pages):
                    raise ValueError(f"{at} variant {v.id!r}: its redirect is one of the experiment's own pages")
        return exp

    @staticmethod
    def _variants(raw, at: str) -> tuple[Variant, ...]:
        if not isinstance(raw, list) or not 2 <= len(raw) <= MAX_VARIANTS:
            raise ValueError(f"{at}: give 2 to {MAX_VARIANTS} variants, the original first")
        out = tuple(Variant.parse(v, at) for v in raw)
        if len({v.id for v in out}) != len(out):
            raise ValueError(f"{at}: two variants share an id")
        return out

    def to_data(self) -> dict:
        out: dict = {"key": self.key, "status": self.status, "goal": self.goal.text}
        if self.name:
            out["name"] = self.name
        for k, default in (("traffic", 1.0), ("allocation", "split"), ("explore", 0.1), ("audience", "everyone"),
                           ("winner", ""), ("confidence", 0.95)):
            if getattr(self, k) != default:
                out[k] = getattr(self, k)
        if self.metrics:
            out["metrics"] = [m.text for m in self.metrics]
        if self.pages:
            out["pages"] = [p.text for p in self.pages]
        if self.multivariate:
            out["factor"] = [{"id": f.id, "variant": [v.to_data() for v in f.variants]} for f in self.factors]
        else:
            out["variant"] = [v.to_data() for v in self.factors[0].variants]
        return out


# Config field -> (its environment variable, its SSM parameter's name under SECRETS_PATH).
SECRETS = {
    "origin_secret": ("ORIGIN_SECRET", "origin-secret"),
    "session_secret": ("SESSION_SECRET", "session-secret"),
    "server_secret": ("SERVER_SECRET", "server-secret"),
    "client_secret": ("OIDC_CLIENT_SECRET", "oidc-client-secret"),
}


def ssm_secrets(path: str) -> dict[str, str]:
    """The secrets under `path` in SSM Parameter Store, by config field: one call, once a
    cold start. A missing parameter is simply absent."""
    import boto3

    names = {f"{path.rstrip('/')}/{name}": field for field, (_, name) in SECRETS.items()}
    got = boto3.client("ssm").get_parameters(Names=sorted(names), WithDecryption=True)
    return {names[p["Name"]]: p["Value"] for p in got.get("Parameters", []) if p.get("Name") in names}


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
    server_ip: str = "bots"          # the server request log's addresses: "bots" (whole for bots,
                                     # a person's cut to its /24 or /48), "full" or "truncate"
    dev_url: str = ""                # its local dev server, for links and the CSP
    client: bool = True              # its pages load c.js; false = server logs only
    funnels: tuple[Funnel, ...] = () # [[site.funnel]]: the console's Funnels tab
    consent_docs: str = ""           # "/privacy/v{version}": each consent version's policy, for the console
    experiments: tuple[Experiment, ...] = ()   # [[site.experiment]]: A/B, multivariate and bandit tests

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
                distribution=s.get("distribution", ""), server_ip=s.get("server_ip", "bots"),
                dev_url=s.get("dev_url", ""), client=bool(s.get("client", True)),
                funnels=tuple(Funnel.parse(f, s["id"]) for f in s.get("funnel", ())),
                consent_docs=s.get("consent_docs", ""),
                experiments=tuple(Experiment.parse(e, s["id"]) for e in s.get("experiment", ())),
            )
            if site.server_ip not in ("bots", "full", "truncate"):
                raise ValueError(f"{site.id}: server_ip must be 'bots', 'full' or 'truncate'")
            if site.consent_docs and not (site.consent_docs.startswith("/") and "{version}" in site.consent_docs):
                raise ValueError(f"{site.id}: consent_docs is a path with {{version}} in it, like /privacy/v{{version}}")
            names = [f.name for f in site.funnels]
            if len(names) != len(set(names)):
                raise ValueError(f"{site.id}: two funnels share a name")
            keys = [e.key for e in site.experiments]
            if len(keys) != len(set(keys)):
                raise ValueError(f"{site.id}: two experiments share a key")
            if site.experiments and not site.client:
                raise ValueError(f"{site.id}: experiments run in c.js, and this site's pages don't load it")
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
    def from_env(cls, env=None, fetch=None) -> "Config":
        """On Lambda. Secrets come from SSM Parameter Store when `SECRETS_PATH` names where
        they are (infra/deploy.py), so the function's configuration, which a deploy role
        can read, holds none of them; an environment variable of the same name is the
        fallback (a function provisioned before this, and local runs)."""
        env = os.environ if env is None else env
        data = json.loads(env.get("CARTERLITICS_CONFIG") or "{}")
        ids = {i.strip() for i in env.get("ADMIN_IDS", "").split(",") if i.strip()}
        kept = (fetch or ssm_secrets)(env["SECRETS_PATH"]) if env.get("SECRETS_PATH") else {}
        found = {field: kept.get(field) or env.get(var, "") for field, (var, _) in SECRETS.items()}
        cfg = cls.from_data(data, client_id=env.get("OIDC_CLIENT_ID", ""), **found)
        if ids:
            cfg = cls(**{**cfg.__dict__, "admin_ids": cfg.admin_ids | ids})
        if PACKAGED.exists():
            cfg = cfg.with_experiments(json.loads(PACKAGED.read_text(encoding="utf-8")))
        return cfg

    def with_experiments(self, data: dict) -> "Config":
        """The config with each site's experiments from `experiments_data()`'s shape: what
        the code package carries (`experiments.json`). A site it doesn't know is skipped."""
        sites = dict(self.sites)
        for site_id, items in (data or {}).items():
            if site_id in sites:
                sites[site_id] = dataclasses.replace(
                    sites[site_id], experiments=tuple(Experiment.parse(e, site_id) for e in items))
        return dataclasses.replace(self, sites=sites)

    def experiments_data(self) -> dict:
        """Every site's experiments, as `infra/deploy.py` writes them into the code package."""
        return {s.id: [e.to_data() for e in s.experiments] for s in self.sites.values() if s.experiments}

    def to_data(self) -> dict:
        """The non-secret part, as the deploy script hands it to Lambda. Experiments go in
        the code package instead (`experiments_data`): with their CSS and copy, they would
        outgrow the 4 KB Lambda allows its environment. A site's fields at their defaults
        (`from_data` fills them back in) are left out for the same reason: Lambda counts the
        JSON's escaped quotes, and the sites alone had come within a few bytes of the limit."""
        return {
            "console": self.console, "mount": self.mount, "retention": self.retention,
            "auth": {"issuer": self.issuer, "admin_role": self.admin_role,
                     "admin_ids": sorted(self.admin_ids)},
            **({"google": {"account": self.google_account}} if self.google_account else {}),
            "site": [{"id": s.id,
                      **({"url": s.url} if s.url != f"https://{s.id}" else {}),
                      **({"aliases": list(s.aliases)} if s.aliases else {}),
                      **({"exclude": list(s.exclude)} if s.exclude else {}),
                      **({"server_ip": s.server_ip} if s.server_ip != "bots" else {}),
                      **({"consent_docs": s.consent_docs} if s.consent_docs else {}),
                      **({"funnel": [f.to_data() for f in s.funnels]} if s.funnels else {})}
                     for s in self.sites.values()],
        }
