# carterlitics

First-party, self-hosted analytics for a handful of sites: who visits, which bots crawl,
what breaks, and (with consent) what a visit looked like. One collector serves every
site, so cartercole.com and cartercole.net share the code, the storage and the console.

- **Server side, every request.** CloudFront's access logs flow into the collector, so
  every page request is counted, cache hits included: people, crawlers, AI bots,
  scanners probing `/wp-login.php`, feed readers. Each comes with its IP, user agent,
  country and network.
- **Client side, one script.** `/_c/c.js` counts page views (SPA navigations too),
  engaged time, unique visitors (cookieless, a daily-salted hash), custom events
  (`cl("track", …)`, awaitable), and JavaScript errors: uncaught, unhandled rejections,
  failed resources, CSP violations, grouped by bug.
- **With consent, more.** A visitor cookie, a session, a device fingerprint, the IP and
  user agent per page view, and an rrweb session recording you can replay.
- **One console** at `/_c/admin/`: overview, traffic, bots, visitors and sessions,
  events, errors, replays, for every site.

Stdlib Python on Lambda (no dependencies beyond the runtime's boto3), one DynamoDB table,
two S3 buckets. No build step for the browser side.

```
carterlitics/        the collector (a Python package; Lambda handler carterlitics.app.lambda_handler)
  app.py             routes: beacons, static files, the console and its API, S3 log events
  store.py           every read and write, and the key layout (see its docstring)
  classify.py        bots, browsers, page paths, referrers, datacenter networks
  logs.py            CloudFront access logs (v2 JSON or W3C) -> records
  geo.py             a pure-Python .mmdb reader: IPinfo Lite, GeoLite2 Country/City/ASN
  auth.py            console sign-in: OpenID Connect + PKCE, admins only; server keys
  config.py          sites, retention, secrets
  backends.py        DynamoDB and S3, and in-memory twins for tests and dev
  dev.py, demo.py    the local server, and 30 days of fake traffic for it
  static/
    c.js             the client
    console.*        the console; replay.* the player page
    vendor/          rrweb 2.2.0 recorder and player (MIT)
carterlitics.toml    the sites, retention, sign-in, and [aws] names
infra/deploy.py      provision, ship, route, enable logging (AWS CLI, idempotent)
tests/               python3 -m unittest discover tests
```

## How it fits together

```
                       ┌──────────── cartercole.com's CloudFront ──────────┐
browser ── /_c/c.js ──►│  /_c/c.js, /_c/s/*   cached (CachingOptimized)     │
        ── /_c/i ─────►│  /_c/*               uncached, + x-origin-verify,  │──► Lambda carterlitics
        ── /_c/r ─────►│                      x-carterlitics-site headers   │       │  (Function URL)
                       └──────────── access logs (v2, JSON) ───────────────┘       │
                                         │                                         ├─► DynamoDB carterlitics
                       ┌── every other site's CloudFront, the same ──┐              │     counters, logs, sessions,
                       └─────────────────────────────────────────────┘              │     error groups (TTL)
                                         ▼                                         ├─► S3 carterlitics-<account>
                          S3 carterlitics-<account>-logs ── ObjectCreated ─────────┘     rec/<site>/<session>/
```

Every site routes `/_c/*` to the same Function URL. CloudFront adds two headers: a
shared secret (`x-origin-verify`, without which the collector answers 403) and the
site's id (`x-carterlitics-site`). Viewers can't forge either, since CloudFront overwrites a
viewer's header of the same name. Being on each site's own origin keeps everything
first-party: cookies are the site's, CloudFront supplies the real viewer address,
country and ASN, and blockers that target third-party analytics hosts don't apply.

## What is collected

| | When | What | Kept |
|---|---|---|---|
| Server requests | always | IP, user agent, method, path, query, status, referrer, country, network, edge result. Assets (`.js`, `.css`, images, fonts) are skipped; feeds, sitemaps and `robots.txt` are kept | 30 days |
| Page counters | always, unless DNT/GPC | daily counts per page, referrer, country, device size, browser, OS; engaged (visible) time per page; bots that run JS are counted apart | 400 days |
| Unique visitors | always, unless DNT/GPC | `sha256(daily salt, site, IP, UA)`; the salt is random and deleted after 2 days, so hashes can't be reversed afterwards | 400 days (as a count) |
| Events | always, unless DNT/GPC | name, properties (≤ 2 KB), page | 180 days |
| Errors | always | message, type, stack, source, page, user agent; no identifier unless consented | 90 days |
| Visits | consent only | visitor cookie (`vid`), session, fingerprint, IP, UA, geo, per page view | 400 days |
| Recordings | consent only | rrweb: DOM, mouse, scroll; inputs masked, `[data-private]` blocked, `[data-mask]` text masked | 30 days |

Retention is `[retention]` in `carterlitics.toml`; everything expires through the table's
TTL or a bucket lifecycle rule. A site with `server_ip = "truncate"` keeps server-log
addresses at /24 (IPv4) and /48 (IPv6). Query strings in the request log have the values
of credential-like parameters (`code`, `state`, `token`, signatures, …) replaced, since
OAuth callbacks carry them.

## The sites

`carterlitics.toml` lists cartercole.com and cartercole.net (script, consent banner,
recordings), the project sites under cartercole.net (script in anonymous mode: they have
no banner, so page views, events and errors, nothing identifying), and four hosts counted
from CloudFront's logs alone (`client = false`): the account hub, the power app, and the
two `*.` catch-all redirects. A site is matched to its distributions by host name, so
`resume` covers both resume.cartercole.net and resume.cartercole.com.

**Bots** are recognised by user agent (about 150 named crawlers and tools, by kind: search, AI,
social, SEO, monitoring, feeds, scanners, automation and HTTP libraries), by asking for
something only a scanner wants (`.php`, `/.env`, `/.git/`, `/wp-…`), by sending no user
agent or a non-browser one, and, for scripts that run, by `navigator.webdriver`. A
browser user agent from a hosting network (AWS, GCP, DigitalOcean, Hetzner, …) is
flagged "datacenter" rather than called a bot, since it may be a VPN.

## Using it on a site

1. Add the site to `carterlitics.toml` (`id`, `url`, `aliases`, `exclude`, `dev_url`) and
   re-run `infra/deploy.py provision`.
2. Put the script and a banner in the page layout:

   ```html
   <script async src="/_c/c.js"></script>

   <!-- c.js shows this if the visitor hasn't chosen, and hides it when they do -->
   <div data-consent hidden>
     <p>… what you collect, a link to /privacy …</p>
     <button type="button" data-choice="essential">Anonymous only</button>
     <button type="button" data-choice="all">Accept</button>
   </div>
   <button type="button" data-consent-reopen>Privacy choices</button>
   ```

   Style the banner however the site looks; only the data attributes matter. Without a
   banner the script stays anonymous: `<script async src="/_c/c.js"></script>` alone is a
   complete install.
3. `infra/deploy.py routes <site>` and `infra/deploy.py logging <site>`.

Script options (attributes on the tag): `data-mode="consent"` (default) · `"full"`
(identify without asking: an internal tool) · `"anonymous"` (never identify, no
banner); `data-record="off"`; `data-errors="off"`; `data-exclude="/admin,/auth"`.

### From JavaScript

```html
<!-- optional: lets you call cl() before c.js has loaded -->
<script>window.cl=window.cl||function(){(cl.q=cl.q||[]).push(arguments)}</script>
```

```js
await cl("track", "download", { file: "mosaic.zip" });  // resolves once it's sent
cl("error", err, { where: "checkout" });                // a caught error, reported anyway
cl("page");                                              // a page view by hand (pushState is automatic)
cl("consent", "all");                                    // what the banner's buttons do
cl("identify", "user-123");                              // tie a consented visitor to your user id
carterlitics.flush();                                    // send what's queued now
```

Events and errors are batched for a second, then sent with `fetch(keepalive)`, or
`sendBeacon` when the page is going away.

### From a site's backend

```bash
curl -X POST https://cartercole.com/_c/server/events \
  -H "x-carterlitics-key: $(AWS_PROFILE=carter-admin infra/deploy.py server-key cartercole.com)" \
  -d '{"events":[{"name":"signup","props":{"via":"google"},"uid":"user-123","ip":"203.0.113.9","ua":"Mozilla/5.0 …","path":"/auth/google"}]}'
```

The key is derived per site from one secret, so a leaked key only speaks for its own site.

## The console

`https://cartercole.net/_c/admin/` (the `console` site; `/_c/admin/` on any other site
redirects there). Sign-in is OpenID Connect at accounts.cartercole.net with PKCE; only an
account with the hub's `god` role, or whose `sub` is in `[auth].admin_ids` or the
`/carterlitics/admin-ids` parameter, gets a session (`cl_session`, 7 days, HMAC-signed,
`Path=/_c/`), and that is checked again on every request. Console pages carry
`noindex` and a Content-Security-Policy that lets a replayed recording load only the
sites' own files. Visitor-supplied strings reach the page only through `textContent`.

## Develop

```bash
python3 -m carterlitics.dev --demo         # http://127.0.0.1:8795/_c/admin/, 30 days of fake data
python3 -m unittest discover tests         # GEO_MMDB=path/to/file.mmdb also tests the geo reader
```

The dev server keeps everything in memory, signs you straight in as an administrator,
and accepts `POST /_c/dev/logs` with CloudFront log text, to see the server-side reports
without AWS. `GEO_MMDB` (comma-separated `.mmdb` paths) turns on network names.

Each site's own dev server forwards `/_c/*` here the way CloudFront does:
cartercole.net's `uv run build.py --serve` always does, and cartercole.com's frontend
does when started with `CARTERLITICS_URL=http://127.0.0.1:8795`. `.claude/launch.json`
starts all three.

## Deploy

Everything below runs under `AWS_PROFILE=carter-admin` and is safe to re-run.
`--dry-run` prints what would change.

```bash
infra/deploy.py register-client                 # OIDC client at accounts.cartercole.net (over ssh gputy)
infra/deploy.py geo                             # optional: IPinfo Lite into geo/ (IPINFO_TOKEN)
infra/deploy.py provision                       # table, buckets, role, function, URL, secrets, log trigger
infra/deploy.py routes cartercole.net --dry-run --drop '/api/*' '/auth/*'
infra/deploy.py routes cartercole.net --drop '/api/*' '/auth/*'   # replaces the old per-site API routes
infra/deploy.py routes all                      # /_c/* on every site with client = true
infra/deploy.py logging all                     # CloudFront access logs -> collector, every site
infra/deploy.py grant-deploy                    # let CI and site-publisher run `code`
```

Then deploy each site (its `deploy.sh`) so its pages load `/_c/c.js`. After that,
`infra/deploy.py code` ships a changed collector: a deterministic zip of the package (and
any `.mmdb` in `geo/`), uploaded only when its SHA-256 differs from what Lambda runs.

Buckets are `carterlitics-<account id>` and `…-logs` unless `[aws]` names them. `routes`
adds the `carterlitics` origin and three behaviors at the front of the
distribution and leaves the rest alone. It keeps the first config it saw in
`infra/.state/` for `routes <site> --rollback`. cartercole.com's `infra/cutover.sh` keeps
these routes when it rewrites its distribution.

### Retiring the per-site analytics

Both sites ran their own analytics before this: cartercole.net's `cartercole-net-api`
Lambda behind `/api/*` and `/auth/*`, and cartercole.com's GraphQL mutations. Both sites
now load `/_c/c.js`, and the code is gone from both repositories.

- cartercole.net: `/api/*` and `/auth/*` were removed from its distribution
  (`routes cartercole.net --drop`). The old Lambda, its table, bucket, role, parameters,
  origin request policy, the unused `api` origin and its hub client are being retired
  separately.
- cartercole.com: the table `cartercole-analytics` stays in its stack with
  `DeletionPolicy: Retain`. Delete the block after a deploy to orphan it, then the table
  by hand if its few days of counts aren't wanted. The parameters
  `/cartercole/auth/admin-token` and `admin-ids` are no longer read.

## Cost

At personal-site traffic, all inside free tiers or cents: a page view is about ten
DynamoDB writes on demand, a log file is one Lambda run, and a recording is an S3 PUT per
5 seconds of activity, gzipped. The function is 512 MB, arm64.
