"""What a request is: a bot or a person, which browser, which page, which referrer.

Everything here is pure and cheap, so the collector runs it on every beacon and every
CloudFront log line. Nothing here touches storage.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------- bots

# (category, display name, lowercase UA substring). First match wins, so specific
# names come before the generic catch-alls at the bottom.
BOTS: tuple[tuple[str, str, str], ...] = (
    # AI crawlers and assistants
    ("ai", "GPTBot", "gptbot"),
    ("ai", "ChatGPT-User", "chatgpt-user"),
    ("ai", "OAI-SearchBot", "oai-searchbot"),
    ("ai", "ClaudeBot", "claudebot"),
    ("ai", "Claude-User", "claude-user"),
    ("ai", "Claude-SearchBot", "claude-searchbot"),
    ("ai", "Claude-Web", "claude-web"),
    ("ai", "anthropic-ai", "anthropic-ai"),
    ("ai", "PerplexityBot", "perplexitybot"),
    ("ai", "Perplexity-User", "perplexity-user"),
    ("ai", "CCBot", "ccbot"),
    ("ai", "Bytespider", "bytespider"),
    ("ai", "Amazonbot", "amazonbot"),
    ("ai", "Meta-ExternalAgent", "meta-externalagent"),
    ("ai", "Meta-ExternalFetcher", "meta-externalfetcher"),
    ("ai", "cohere-ai", "cohere-ai"),
    ("ai", "Diffbot", "diffbot"),
    ("ai", "YouBot", "youbot"),
    ("ai", "AI2Bot", "ai2bot"),
    ("ai", "Timpibot", "timpibot"),
    ("ai", "DuckAssistBot", "duckassistbot"),
    ("ai", "MistralAI-User", "mistralai-user"),
    ("ai", "ImagesiftBot", "imagesiftbot"),
    ("ai", "Applebot-Extended", "applebot-extended"),
    ("ai", "Google-CloudVertexBot", "google-cloudvertexbot"),
    # search engines
    ("search", "Googlebot", "googlebot"),
    ("search", "Google-InspectionTool", "google-inspectiontool"),
    ("search", "GoogleOther", "googleother"),
    ("search", "Storebot-Google", "storebot-google"),
    ("search", "AdsBot-Google", "adsbot-google"),
    ("search", "Bingbot", "bingbot"),
    ("search", "BingPreview", "bingpreview"),
    ("search", "DuckDuckBot", "duckduckbot"),
    ("search", "Applebot", "applebot"),
    ("search", "YandexBot", "yandex"),
    ("search", "Baiduspider", "baiduspider"),
    ("search", "PetalBot", "petalbot"),
    ("search", "SeznamBot", "seznambot"),
    ("search", "Sogou", "sogou"),
    ("search", "Qwantbot", "qwant"),
    ("search", "Mojeek", "mojeekbot"),
    ("search", "Yahoo Slurp", "slurp"),
    ("search", "Exabot", "exabot"),
    ("search", "Kagi", "kagibot"),
    ("search", "Brave", "bravebot"),
    # link previews and social
    ("social", "Facebook", "facebookexternalhit"),
    ("social", "Facebook", "facebot"),
    ("social", "Twitterbot", "twitterbot"),
    ("social", "LinkedInBot", "linkedinbot"),
    ("social", "Slackbot", "slackbot"),
    ("social", "Slack", "slack-imgproxy"),
    ("social", "Discordbot", "discordbot"),
    ("social", "TelegramBot", "telegrambot"),
    ("social", "WhatsApp", "whatsapp"),
    ("social", "Pinterest", "pinterest"),
    ("social", "Redditbot", "redditbot"),
    ("social", "Skype", "skypeuripreview"),
    ("social", "Iframely", "iframely"),
    ("social", "Embedly", "embedly"),
    ("social", "Mastodon", "mastodon"),
    ("social", "Bluesky", "bluesky"),
    ("social", "Snapchat", "snapchat"),
    ("social", "Google Read Aloud", "google-read-aloud"),
    ("social", "Apple iMessage", "facebookexternalhit"),
    # SEO and marketing crawlers
    ("seo", "AhrefsBot", "ahrefs"),
    ("seo", "SemrushBot", "semrush"),
    ("seo", "MJ12bot", "mj12bot"),
    ("seo", "DotBot", "dotbot"),
    ("seo", "rogerbot", "rogerbot"),
    ("seo", "Screaming Frog", "screaming frog"),
    ("seo", "SerpstatBot", "serpstatbot"),
    ("seo", "DataForSeoBot", "dataforseo"),
    ("seo", "BLEXBot", "blexbot"),
    ("seo", "Barkrowler", "barkrowler"),
    ("seo", "SeekportBot", "seekport"),
    ("seo", "MegaIndex", "megaindex"),
    ("seo", "Majestic", "majestic"),
    # uptime and monitoring
    ("monitor", "UptimeRobot", "uptimerobot"),
    ("monitor", "Pingdom", "pingdom"),
    ("monitor", "StatusCake", "statuscake"),
    ("monitor", "Site24x7", "site24x7"),
    ("monitor", "Better Stack", "betteruptime"),
    ("monitor", "Better Stack", "better stack"),
    ("monitor", "HetrixTools", "hetrixtools"),
    ("monitor", "Freshping", "freshping"),
    ("monitor", "Checkly", "checkly"),
    ("monitor", "updown.io", "updown.io"),
    ("monitor", "Datadog", "datadog"),
    ("monitor", "New Relic", "newrelicpinger"),
    ("monitor", "cron-job.org", "cron-job.org"),
    ("monitor", "Amazon Route 53", "amazon-route53"),
    ("monitor", "Amazon CloudFront", "amazon cloudfront"),
    # feed readers
    ("feed", "Feedly", "feedly"),
    ("feed", "Feedbin", "feedbin"),
    ("feed", "Inoreader", "inoreader"),
    ("feed", "NewsBlur", "newsblur"),
    ("feed", "The Old Reader", "theoldreader"),
    ("feed", "Feedspot", "feedspot"),
    ("feed", "Miniflux", "miniflux"),
    ("feed", "NetNewsWire", "netnewswire"),
    ("feed", "Tiny Tiny RSS", "tiny tiny rss"),
    ("feed", "FreshRSS", "freshrss"),
    ("feed", "Feedfetcher", "feedfetcher"),
    ("feed", "Reeder", "reeder"),
    # internet scanners
    ("scanner", "Censys", "censys"),
    ("scanner", "zgrab", "zgrab"),
    ("scanner", "masscan", "masscan"),
    ("scanner", "Nmap", "nmap"),
    ("scanner", "Nuclei", "nuclei"),
    ("scanner", "Expanse", "expanse"),
    ("scanner", "InternetMeasurement", "internet-measurement"),
    ("scanner", "Shodan", "shodan"),
    ("scanner", "LeakIX", "l9explore"),
    ("scanner", "LeakIX", "leakix"),
    ("scanner", "Nikto", "nikto"),
    ("scanner", "sqlmap", "sqlmap"),
    ("scanner", "WPScan", "wpscan"),
    ("scanner", "Palo Alto Networks", "paloaltonetworks"),
    ("scanner", "Netcraft", "netcraft"),
    ("scanner", "ODIN", "odin.io"),
    ("scanner", "ModatScanner", "modat"),
    # automation and HTTP libraries
    ("automation", "HeadlessChrome", "headlesschrome"),
    ("automation", "PhantomJS", "phantomjs"),
    ("automation", "Puppeteer", "puppeteer"),
    ("automation", "Playwright", "playwright"),
    ("automation", "Selenium", "selenium"),
    ("automation", "Lighthouse", "lighthouse"),
    ("automation", "PageSpeed", "pagespeed"),
    ("automation", "GTmetrix", "gtmetrix"),
    ("library", "curl", "curl/"),
    ("library", "Wget", "wget"),
    ("library", "python-requests", "python-requests"),
    ("library", "python-urllib", "python-urllib"),
    ("library", "aiohttp", "aiohttp"),
    ("library", "httpx", "python-httpx"),
    ("library", "Go", "go-http-client"),
    ("library", "Java", "java/"),
    ("library", "okhttp", "okhttp"),
    ("library", "Apache HttpClient", "apache-httpclient"),
    ("library", "node-fetch", "node-fetch"),
    ("library", "undici", "undici"),
    ("library", "axios", "axios"),
    ("library", "Node.js", "node.js"),
    ("library", "libwww-perl", "libwww-perl"),
    ("library", "Ruby", "ruby"),
    ("library", "Guzzle", "guzzlehttp"),
    ("library", "PHP", "php/"),
    ("library", "Postman", "postmanruntime"),
    ("library", "Insomnia", "insomnia"),
    ("library", "HTTPie", "httpie"),
    ("library", "Scrapy", "scrapy"),
    ("library", "Colly", "colly"),
    ("library", "reqwest", "reqwest"),
    ("library", "Dart", "dart:io"),
    ("library", "Deno", "deno/"),
    ("library", "Bun", "bun/"),
    # anything that says what it is
    ("other", "other bot", "bot"),
    ("other", "other crawler", "crawl"),
    ("other", "other spider", "spider"),
    ("other", "other scraper", "scrap"),
    ("other", "other fetcher", "fetch"),
    ("other", "other preview", "preview"),
    ("other", "other monitor", "monitor"),
    ("other", "other checker", "check"),
    ("other", "other archiver", "archive"),
)

# Requests no visitor makes on a site that isn't WordPress, PHP, or a git checkout:
# whoever sends them is scanning, whatever their user agent says.
PROBE = re.compile(
    r"(?i)(\.php\d?$|\.aspx?$|\.jsp$|\.cgi$|/wp-|/wordpress|/xmlrpc|/\.env|/\.git|/\.aws|/\.ssh"
    r"|/\.vscode|/\.ds_store|/phpmyadmin|/pma/|/adminer|/cgi-bin|/vendor/phpunit|/actuator"
    r"|/server-status|/config\.(json|ya?ml)$|/\.well-known/security\.txt\.bak|/boaform|/hnap1"
    r"|/owa/|/autodiscover|/ecp/|/remote/login|/geoserver|/solr/|/console/|/telescope|/_ignition"
    r"|/\.htaccess|/\.htpasswd|/backup|\.sql$|\.bak$|\.old$|\.tar(\.gz)?$|\.zip$|/shell|/eval)"
)

# Hosting and cloud networks. A browser user agent from one of these is most often a
# script; it can also be a VPN, so it is reported as "datacenter", not counted as a bot.
DATACENTER = (
    "amazon", "aws", "google", "microsoft", "azure", "digitalocean", "linode", "akamai",
    "ovh", "hetzner", "vultr", "choopa", "contabo", "scaleway", "online s.a.s", "leaseweb",
    "alibaba", "tencent", "oracle", "ibm", "cloudflare", "fastly", "m247", "datacamp",
    "hostinger", "ionos", "1&1", "godaddy", "hostwinds", "colocrossing", "quadranet",
    "psychz", "servers.com", "packet", "equinix", "zenlayer", "cogent", "g-core", "gcore",
    "huawei", "kamatera", "upcloud", "netcup", "aeza", "pq hosting", "stark industries",
    "frantech", "buyvm", "ponynet", "selectel", "timeweb", "hivelocity", "limestone",
    "liquid web", "rackspace", "softlayer", "dreamhost", "a2 hosting", "hurricane electric",
    "internet archive", "censys", "shodan", "driftnet", "constantine cybersecurity",
)


@dataclass(frozen=True)
class Bot:
    name: str
    category: str


def bot_of(user_agent: str | None, path: str | None = None) -> Bot | None:
    """The bot behind a request, or None for what looks like a person.

    No user agent at all is a bot: every browser sends one. A probe path is a scanner
    whatever the user agent claims."""
    ua = (user_agent or "").strip()
    if not ua or ua == "-":
        return Bot("(no user agent)", "other")
    low = ua.lower()
    for category, name, needle in BOTS:
        if needle in low:
            return Bot(name, category)
    if path and PROBE.search(path):
        return Bot("probe", "scanner")
    # Real browsers all claim Mozilla/5.0 (or Opera/). Anything else is a program.
    if not low.startswith(("mozilla/", "opera/")):
        return Bot(ua.split("/")[0].split(" ")[0][:40] or "(unknown)", "other")
    return None


def is_datacenter(network: str | None) -> bool:
    low = (network or "").lower()
    return any(d in low for d in DATACENTER)


# ---------------------------------------------------------------- browsers


def browser_of(user_agent: str | None) -> tuple[str, str]:
    """(browser, operating system) families, for counting. Not for anything precise."""
    ua = user_agent or ""
    if "Edg/" in ua or "EdgA/" in ua or "EdgiOS/" in ua:
        browser = "Edge"
    elif "OPR/" in ua or "Opera" in ua:
        browser = "Opera"
    elif "SamsungBrowser/" in ua:
        browser = "Samsung Internet"
    elif "Vivaldi/" in ua:
        browser = "Vivaldi"
    elif "DuckDuckGo/" in ua or "Ddg/" in ua:
        browser = "DuckDuckGo"
    elif "Firefox/" in ua or "FxiOS/" in ua:
        browser = "Firefox"
    elif "CriOS/" in ua or "Chrome/" in ua or "Chromium/" in ua:
        browser = "Chrome"
    elif "Safari/" in ua and "Version/" in ua:
        browser = "Safari"
    elif "AppleWebKit/" in ua and ("iPhone" in ua or "iPad" in ua):
        browser = "Safari (in-app)"
    else:
        browser = "Other"

    if "iPhone" in ua or "iPad" in ua or "iPod" in ua:
        system = "iOS"
    elif "Android" in ua:
        system = "Android"
    elif "CrOS" in ua:
        system = "ChromeOS"
    elif "Mac OS X" in ua or "Macintosh" in ua:
        system = "macOS"
    elif "Windows" in ua:
        system = "Windows"
    elif "Linux" in ua or "X11" in ua:
        system = "Linux"
    else:
        system = "Other"
    return browser, system


def device_of(width) -> str:
    if not isinstance(width, (int, float)) or isinstance(width, bool) or width <= 0:
        return "unknown"
    return "mobile" if width < 700 else "tablet" if width < 1100 else "desktop"


# ---------------------------------------------------------------- pages


PATH_CHARS = re.compile(r"[A-Za-z0-9/_.\-~%@+]+")


def clean_path(path, exclude: tuple[str, ...] = ()) -> str | None:
    """One page, one row: `/x`, `/x/`, `/x.html` and `/x/index.html` all count as `/x`.

    None for anything that isn't a page path, or that the site excludes (an admin
    area, its API). `exclude` entries are prefixes matched on whole segments."""
    if not isinstance(path, str) or not path.startswith("/") or len(path) > 200:
        return None
    path = path.split("?", 1)[0].split("#", 1)[0]
    if not PATH_CHARS.fullmatch(path) or "//" in path:
        return None
    if path.endswith("/index.html"):
        path = path[: -len("index.html")]
    elif path.endswith(".html"):
        path = path[:-5]
    path = path.rstrip("/") or "/"
    if path == "/index":
        path = "/"
    if path == "/_c" or path.startswith("/_c/"):
        return None
    for prefix in exclude:
        prefix = prefix.rstrip("/")
        if prefix and (path == prefix or path.startswith(prefix + "/")):
            return None
    return path


ASSET = re.compile(
    r"(?i)\.(css|js|mjs|map|png|jpe?g|gif|webp|avif|svg|ico|bmp|woff2?|ttf|otf|eot|mp4|webm|mov"
    r"|mp3|ogg|wav|flac|m4a|wasm|pdf|webmanifest)$"
)


def is_asset(path: str) -> bool:
    """Files a page pulls in. Server-side counting skips them: they say nothing a page
    request doesn't, and they are most of the traffic. Feeds, sitemaps and robots.txt
    are kept, since who fetches them is the interesting part."""
    return bool(ASSET.search(path or ""))


def host_of(url) -> str | None:
    """`https://www.Example.com:8080/x?y` -> `example.com`."""
    if not isinstance(url, str) or "://" not in url:
        return None
    rest = url.split("://", 1)[1]
    host = re.split(r"[/?#]", rest, maxsplit=1)[0].rsplit("@", 1)[-1]
    host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    host = host.lower().strip("[]").removeprefix("www.")
    return host[:80] or None


def referrer_of(url, own_hosts: frozenset[str]) -> str:
    """The referring site, `direct` for none, and none for the site itself: internal
    navigation is not a referral."""
    host = host_of(url)
    if not host or host in own_hosts:
        return "direct"
    return host


# ---------------------------------------------------------------- identifiers


ID = re.compile(r"[0-9a-fA-F-]{8,64}")
NAME = re.compile(r"[A-Za-z0-9_.:\- ]{1,64}")
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def valid_id(value) -> bool:
    """Browser-made identifiers: hex and dashes only, so they can't smuggle key separators."""
    return isinstance(value, str) and ID.fullmatch(value) is not None


def valid_name(value) -> bool:
    return isinstance(value, str) and NAME.fullmatch(value) is not None and value.strip() == value


def text(value, limit: int = 500) -> str:
    """A visitor-supplied string, cut to size. Anything that isn't a string is empty."""
    return value[:limit] if isinstance(value, str) else ""


def dim(value: str, limit: int = 120) -> str:
    """A value safe to use as the tail of a sort key: no control characters."""
    return "".join(c for c in value if c.isprintable())[:limit]
