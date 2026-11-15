/* carterlitics — the page's half of first-party analytics. https://github.com/CarterCole/carterlitics

   <script async src="/_c/c.js"></script>

   Always, with no cookie and nothing stored on the device: page views (and SPA
   navigations), engaged time per page, custom events, and JavaScript errors.

   Only after the visitor accepts (the consent banner, or cl("consent", "all")): a
   visitor cookie, a session id, a device fingerprint, and a session recording (rrweb,
   typed text masked, [data-private] blanked, [data-mask] text masked). A session is one
   tab's, in sessionStorage, and ends after 30 minutes without a page view or event. A
   duplicated tab, which starts with a copy of its tab's sessionStorage, asks the site's
   other tabs (BroadcastChannel) and takes a session of its own.

   Do Not Track and Global Privacy Control are a standing "no": nothing is identified
   or recorded, page views and events aren't sent, and the banner never shows. Error
   reports still go, anonymously, so the site can be fixed.

   Signs of automation go with page views, as a bot challenge page would look for them,
   but are only counted: navigator.webdriver, a driver's own globals (Playwright,
   Selenium, PhantomJS, …) and a headless brand make a page view a bot's; a burst of
   clicks (`rapid`) is flagged. Leaving goes out by beacon when the browser's `navigate`
   event says the page is going (a link, a form, back, forward, reload), with where to:
   another page here, or which other site. pagehide covers the rest.

   A page view is a page someone saw. A page the browser prerenders starts nothing until
   it's shown (cl() calls wait in the stub's queue, nothing is sent), and one the
   back/forward cache brings back counts again, as the same Back would without the cache.

   How people fill in forms, always, never what they type: every <form> is watched by
   itself. An attempt at one, from the first field someone clicks into, is sent when it's
   submitted or the page view ends without: the fields reached, in order, and for each
   how long they waited before typing, paused before it and spent in it, whether they
   came back to it, deleted some of it, pasted, were autofilled, were refused by the
   browser's validation or left it empty, and the field they gave up in.

   Options, as attributes on the script tag:
     data-mode="consent"   consent (default) · full (identify without asking) · anonymous
     data-consent-version="2"      raise it when Accept comes to cover more: visitors who
                           accepted an earlier version see the banner again
     data-record="off"     no session recording even with consent
     data-errors="off"     don't report errors
     data-exclude="/admin,/auth"   path prefixes to leave alone entirely
     data-google="off"     ignore Google Analytics calls (see below)
     data-datalayer="dataLayer"    the name gtag.js / GTM were given for their queue
     data-hello="off"      no hello in the browser's console for whoever opens DevTools
     data-forms="off"      don't watch forms (one form: data-cl-form="off" on it)

   API, safe to call before the script loads with this stub in the page:
     <script>window.cl=window.cl||function(){(cl.q=cl.q||[]).push(arguments)}</script>

     cl("track", "signup", { plan: "pro" })   // a Promise once loaded: resolves when sent
     cl("error", err, { where: "checkout" })   // report a caught error
     cl("page")                                 // count a page view by hand
     cl("consent", "all" | "essential")         // what the banner buttons do
     cl("identify", "user-123")                 // tie a consented visitor to your user id
     cl("identify", null)                       // signed out: forget it
     cl("who", function (who) { … })            // what the collector makes of this visitor
     cl("submit", form)                         // a form sent some way the browser doesn't call a submit
     window.carterlitics.flush()                // send what's queued now

   cl("who") asks the collector once a page and is a Promise once loaded; the callback
   works through the stub too. It gets null if the collector can't be reached, else:
     { bot: null | { name: "Googlebot", category: "search", verified: true | false | null },
       net: "isp" | "hosting" | "vpn" | "relay" | "tor" | "crawler",
       service: "Mullvad", listed: false, abuse: [], elsewhere: false,
       country: "US", asn: "7922", network: "Comcast" }

   Google Analytics calls are understood too, so a page written for Google keeps
   working with or without Google's own script. Each of these becomes cl("track"),
   named the way GA4 names it (the action), with the category, label and value as
   event_category, event_label and value:

     gtag("event", "sign_up", { method: "google" })                 // gtag.js (GA4)
     dataLayer.push({ event: "sign_up", method: "google" })          // Tag Manager
     ga("send", "event", "Videos", "play", "intro", 3)               // analytics.js
     _gaq.push(["_trackEvent", "Videos", "play", "intro", 3])        // ga.js, the async snippet

   gtag("event", "page_view", { page_path }), ga("send", "pageview", path) and
   _gaq.push(["_trackPageview", path]) count a page view; a user_id / userId given to
   gtag or ga is cl("identify"). Calls made before this script loads are picked up from
   Google's own queues (dataLayer, ga.q, _gaq), so Google's snippet doubles as the stub.
   event_callback / eventCallback / hitCallback run once the event is sent, or after
   event_timeout (2 s), when Google's script isn't on the page to run them; never twice.
*/
(function () {
  "use strict";

  var w = window, d = document, nav = navigator, loc = location;
  if (w.carterlitics && w.carterlitics.v) return;

  var script = d.currentScript;
  var opts = (script && script.dataset) || {};
  var BASE = (script && script.src ? new URL(script.src, loc.href).pathname : "/_c/c.js").replace(/\/c\.js$/, "");
  var MODE = opts.mode === "full" || opts.mode === "anonymous" ? opts.mode : "consent";
  var RRWEB = "__RRWEB__";
  var RECORD = opts.record !== "off";
  var ERRORS = opts.errors !== "off";
  var GOOGLE = opts.google !== "off";
  var FORMS = opts.forms !== "off";
  var LAYER = /^[A-Za-z_$][\w$]*$/.test(opts.datalayer || "") ? opts.datalayer : "dataLayer";
  // What Accept means, by the site's privacy policy. Raised when it comes to cover more:
  // visitors who accepted an earlier version are asked again. "Anonymous only" stands.
  var CONSENT_VERSION = /^[1-9]\d{0,5}$/.test(opts.consentVersion || "") ? parseInt(opts.consentVersion, 10) : 1;
  var EXCLUDE = (opts.exclude || "").split(",").map(function (s) { return s.trim(); }).filter(Boolean)
    .concat([BASE]);

  var OPTED_OUT = nav.globalPrivacyControl === true || nav.doNotTrack === "1" || w.doNotTrack === "1";
  var SECURE = loc.protocol === "https:" ? "; Secure" : "";
  var UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
  var BEACON_BYTES = 60000;     // sendBeacon and keepalive stop at 64 KiB
  var CHUNK_BYTES = 1800000;    // the collector takes recording chunks up to 2 MB
  var MAX_ITEMS = 50;
  var MAX_ERRORS = 25;
  var MAX_CHUNKS = 400;
  var IDLE = 30 * 60 * 1000;    // without a page view or event, a session ends (store.py's IDLE)

  var queue = [];
  var waiting = [];
  var timer = null;
  var ident = null;             // { vid, sid, sd, fp, uid, fresh } once consented
  var wanted = null;            // the page's user id, given before consent: in memory only
  var current = null;           // the path being viewed
  var lastUrl = d.referrer || "";
  var visibleSince = d.visibilityState === "visible" ? Date.now() : 0;
  var engaged = 0;
  var errors = 0;
  var seen = {};
  var recorder = null;
  var shipped = 0;              // recording chunks this page has sent
  var going = null;             // how the page view is ending, when the navigate event said: { how, to }
  var noted = {};               // the page view's signs of automation: seen,
  var told = {};                // ... and already sent (each goes once a page view)

  /* ---------------------------------------------------------------- storage */

  function cookie(name, value, seconds) {
    if (value === undefined) {
      var hit = d.cookie.split("; ").filter(function (c) { return c.indexOf(name + "=") === 0; })[0];
      return hit ? decodeURIComponent(hit.slice(name.length + 1)) : null;
    }
    d.cookie = name + "=" + encodeURIComponent(value) + "; Max-Age=" + seconds + "; Path=/; SameSite=Lax" + SECURE;
  }

  function session(key, value) {
    try {
      if (value === undefined) return sessionStorage.getItem(key);
      if (value === null) sessionStorage.removeItem(key);
      else sessionStorage.setItem(key, value);
    } catch (e) { /* storage blocked */ }
    return null;
  }

  function excluded(path) {
    return EXCLUDE.some(function (p) { return path === p || path.indexOf(p.replace(/\/$/, "") + "/") === 0; });
  }

  /* ---------------------------------------------------------------- transport */

  function post(path, data, final) {
    var body = JSON.stringify(data);
    var small = body.length < BEACON_BYTES;
    if ((final || d.visibilityState === "hidden") && small && nav.sendBeacon &&
        nav.sendBeacon(BASE + path, new Blob([body], { type: "text/plain" }))) {
      return Promise.resolve();
    }
    // text/plain keeps it a simple request; the collector parses the body regardless.
    return fetch(BASE + path, {
      method: "POST", body: body, keepalive: small, credentials: "omit",
      headers: { "Content-Type": "text/plain" }
    }).then(function () {}, function () {});
  }

  // The browser's time zone: when it keeps a different time from the address's, the
  // visitor is travelling or behind a VPN. Counted, and kept only with consent.
  var ZONE = (function () {
    try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (e) { return ""; }
  })();

  function envelope(items) {
    var e = { v: 1, w: w.innerWidth, items: items };
    if (ZONE) e.tz = ZONE;
    if (nav.webdriver) e.wd = true;
    if (DRIVER) e.auto = DRIVER;
    if (ident) {
      e.vid = ident.vid;
      e.sid = ident.sid;
      e.sd = ident.sd;
      if (ident.fp) e.fp = ident.fp;
      if (ident.uid) e.uid = ident.uid;
      var cv = accepted();
      if (cv) e.cv = cv;
    }
    return e;
  }

  function flush(final) {
    clearTimeout(timer);
    timer = null;
    // A prerendered page not shown yet sends nothing; what's queued goes once it starts.
    if (!started) return Promise.resolve();
    var done = waiting;
    waiting = [];
    var sends = [];
    while (queue.length) {
      var items = queue.splice(0, MAX_ITEMS);
      // Keep each body beacon-sized: halve the batch until it fits.
      while (items.length > 1 && JSON.stringify(envelope(items)).length >= BEACON_BYTES) {
        queue = items.splice(Math.ceil(items.length / 2)).concat(queue);
      }
      sends.push(post("/i", envelope(items), final));
    }
    return Promise.all(sends).then(function () {
      done.forEach(function (resolve) { resolve(); });
    });
  }

  function enqueue(item) {
    if (!item.p) item.p = loc.pathname;
    queue.push(item);
    var sent = new Promise(function (resolve) { waiting.push(resolve); });
    if (item.new) {
      // A session's first item makes its record (browser, device, referrer): send it before
      // a recording chunk gets there and makes a bare one.
      clearTimeout(timer);
      timer = setTimeout(flush, 0);
    } else if (!timer) {
      timer = setTimeout(flush, 1000);
    }
    return sent;
  }

  /* ---------------------------------------------------------------- signs of automation */

  // What a bot challenge page checks before it lets a visitor through, short of the
  // challenge: counted with the page view, never acted on here. cl("who") gives a page
  // that wants to act the verdict. Not DevTools: the check challenge pages used, a logged
  // error whose stack getter only Chrome's DevTools protocol would read, no longer fires
  // (Chromium 145 never reads it, even with the protocol's Runtime domain on).

  // A driver that leaves its own globals on the page, by name. navigator.webdriver covers
  // most of the rest, and the collector knows more by user agent.
  var DRIVERS = [
    ["Playwright", "__playwright__binding__ __pwInitScripts"],
    ["Selenium", "_selenium callSelenium _Selenium_IDE_Recorder __selenium_unwrapped __selenium_evaluate " +
      "__webdriver_evaluate __webdriver_unwrapped __webdriver_script_fn __driver_evaluate __driver_unwrapped " +
      "__fxdriver_evaluate __fxdriver_unwrapped"],
    ["PhantomJS", "callPhantom _phantom"],
    ["Nightmare", "__nightmare"],
    ["Cypress", "Cypress"],
    ["Chrome automation", "domAutomation domAutomationController"]
  ];

  function driver() {
    var found = "";
    each(DRIVERS, function (entry) {
      if (!found && entry[1].split(" ").some(function (k) { return k in w || k in d; })) found = entry[0];
    });
    // ChromeDriver puts cdc_… names on the window and $cdc_… on the document.
    each([w, d], function (o) {
      if (!found && Object.keys(o).some(function (k) { return /^\$?cdc_/.test(k); })) found = "Selenium";
    });
    // Headless Chrome says so in its brands even when its user agent says otherwise.
    var brands = nav.userAgentData && nav.userAgentData.brands;
    if (!found && brands && brands.some(function (b) { return /headless/i.test(b.brand); })) found = "HeadlessChrome";
    return found;
  }

  var DRIVER = driver();

  // A burst: 8 clicks or taps inside 2 seconds. Scripts click that fast, and so does a
  // person clicking something that won't respond, which is worth knowing too.
  var clicks = [];

  function watchClicks() {
    d.addEventListener("click", function (e) {
      var t = e.timeStamp || Date.now();
      clicks.push(t);
      while (clicks.length && t - clicks[0] > 2000) clicks.shift();
      if (clicks.length >= 8) noted.rapid = 1;
    }, true);
  }

  // The page view's signs not sent yet, which are then sent: each once a page view.
  function signs() {
    var out = Object.keys(noted).filter(function (k) { return !told[k]; });
    each(out, function (k) { told[k] = 1; });
    return out.length ? out : null;
  }

  /* ---------------------------------------------------------------- page views */

  function counting() {
    return !OPTED_OUT && current !== null && !excluded(current);
  }

  // The page view so far: engaged time, how it ended (when the page knows), any sign of
  // automation since its page view went, and its forms begun and not sent. `over`: the
  // page view is, rather than the page only hidden.
  function settle(over) {
    // Engaged time: only while the page is visible.
    if (visibleSince) {
      engaged += Date.now() - visibleSince;
      visibleSince = d.visibilityState === "visible" ? Date.now() : 0;
    }
    abandon(over);
    var away = going;
    going = null;
    if (counting()) {
      var item = { t: "leave", p: current }, sig = signs();
      if (engaged >= 1000) item.ms = Math.min(Math.round(engaged), 3600000);
      if (away) item.how = away.how;
      if (away && away.to) item.to = away.to;
      if (sig) item.sig = sig;
      if (item.ms || item.how || item.sig) queue.push(item);
    }
    engaged = 0;
  }

  // Campaign tags on the address, for where a visit came from (classify.source_of), and
  // which ad network's click id is there, if one is: its name only, never its value.
  var TAGS = ["utm_source", "utm_medium", "utm_campaign", "ref"];
  var CLICK_IDS = ["gclid", "gbraid", "wbraid", "msclkid", "fbclid", "igshid", "twclid", "ttclid", "li_fat_id",
    "rdt_cid", "yclid"];

  function tags() {
    var params, out = null;
    try { params = new URL(loc.href).searchParams; } catch (e) { return null; }
    each(TAGS, function (k) {
      var v = params.get(k);
      if (v) (out = out || {})[k] = v.slice(0, 100);
    });
    each(CLICK_IDS, function (k) {
      if (!(out && out.click) && params.has(k)) (out = out || {}).click = k;
    });
    return out;
  }

  function page(path) {
    path = typeof path === "string" ? path : loc.pathname;
    if (path === current) {
      going = null;   // the address changed, the page didn't
      return Promise.resolve();
    }
    if (current !== null) {
      going = going || { how: "page" };   // an SPA's navigation; back or forward if the navigate event said so
      settle(true);
    }
    var referrer = current === null ? d.referrer : lastUrl;
    current = path;
    lastUrl = loc.href;
    visibleSince = d.visibilityState === "visible" ? Date.now() : 0;
    viewAt = Date.now();
    viewAway = 0;
    hiddenAt = visibleSince ? 0 : viewAt;
    noted = {};
    told = {};
    clicks = [];
    leaving = false;
    mark(path);
    if (!counting()) return Promise.resolve();
    var item = { t: "pv", p: path, r: referrer || null }, u = tags();
    if (u) item.u = u;
    return enqueue(interaction(item));
  }

  function watchNavigation() {
    if (opts.spa === "off") return;
    ["pushState", "replaceState"].forEach(function (name) {
      var original = history[name];
      history[name] = function () {
        var out = original.apply(this, arguments);
        page();
        return out;
      };
    });
    w.addEventListener("popstate", function () { page(); });
    // A router built on the Navigation API moves the address without pushState.
    var n = w.navigation;
    if (n && typeof n.addEventListener === "function") n.addEventListener("currententrychange", function () { page(); });
  }

  /* ---------------------------------------------------------------- leaving */

  // The Navigation API's navigate event says the page is going before it goes, and
  // where: a link or form to another page here or to another site, back, forward, a
  // reload. That is when the page view is settled and what's queued goes out by beacon,
  // while the page is whole. pagehide still covers the rest: a closed tab, an address
  // typed in, a browser without the API. A #fragment, a download, a mailto: link or a
  // cancelled navigation isn't leaving.
  function watchLeaving() {
    var n = w.navigation;
    if (!n || typeof n.addEventListener !== "function") return;
    n.addEventListener("navigate", function (e) {
      var to;
      try { to = new URL(e.destination.url); } catch (x) { return; }
      if (e.hashChange || typeof e.downloadRequest === "string" || !/^https?:$/.test(to.protocol)) return;
      var how = to.origin === loc.origin ? "page" : "out";
      if (e.navigationType === "reload") how = "reload";
      else if (e.navigationType === "traverse") {
        // An entry on another site has no index here: most often that's back to a search.
        var at = n.currentEntry ? n.currentEntry.index : -1, there = e.destination.index;
        how = there > -1 && at > -1 && there > at ? "forward" : "back";
      }
      var leaving = { how: how, to: how === "out" ? to.hostname : "" };
      going = leaving;
      if (e.destination.sameDocument) return;   // pushState or popstate: page() settles it
      var from = loc.href;
      function away() {
        if (going !== leaving) return;
        if (e.defaultPrevented || n.transition || loc.href !== from) {
          going = null;
          return;
        }
        leaving = true;
        settle(true);
        ship(true);
        flush(true);
      }
      // Somewhere the page's router can't keep it (another site, a page from the cache):
      // now, before the next page can arrive. Otherwise once every listener has had its
      // say, as the router may take the link in hand (page() settles it) or cancel it; a
      // next page quicker than that gets it from pagehide, `going` and all.
      if (e.canIntercept) setTimeout(away, 0);
      else away();
    });
  }

  /* ---------------------------------------------------------------- events */

  function track(name, props) {
    if (OPTED_OUT || typeof name !== "string" || !name) return Promise.resolve();
    var clean = {};
    if (props && typeof props === "object") {
      Object.keys(props).filter(function (k) {
        return props[k] !== undefined && typeof props[k] !== "function";
      }).slice(0, 30).forEach(function (k) {
        var v = props[k];
        if (v === null || typeof v === "number" || typeof v === "boolean") clean[k] = v;
        else clean[k] = (typeof v === "object" ? safe(v) : String(v)).slice(0, 300);
      });
    }
    var item = { t: "ev", n: name.slice(0, 64), props: clean };
    // Google's non_interaction: it happened, but the visitor didn't do it.
    return enqueue(clean.non_interaction === true ? item : interaction(item));
  }

  /* ---------------------------------------------------------------- forms */

  // How people fill in the site's forms, to make them easier. An attempt at a form begins at
  // the first field someone clicks into, and is one item, sent when it's submitted or when
  // the page view ends without (`abandon`). Per field, in the form's order: how long they
  // waited before typing (`wait`), paused between the field before and this one (`gap`) and
  // spent in it (`ms`), how often they came to it (`n`), how often they deleted some of what
  // they'd typed (`fix`), whether they pasted or the browser autofilled it, how often the
  // browser's validation refused it (`bad`), and whether it had anything in it at the end
  // (`fill`). Never a value, its length or a keystroke: a value is read only for `fill`.
  // Time the page spent hidden doesn't count.
  //
  // A form is named by data-cl-form, its id or name, or the page it posts to; a field by
  // data-cl-field, its name (a radio group's buttons are one field), or its id, else its
  // kind and place. Fields in [data-private] aren't watched; nor is a form with
  // data-cl-form="off". Fields outside a <form> are gathered by a [data-cl-form] box around
  // them; the browser doesn't submit one, so the page says when, with cl("submit", box).
  var MAX_FIELDS = 30;
  var SKIP = { hidden: 1, submit: 1, button: 1, reset: 1, image: 1 };
  var attempts = [];            // this page view's
  var lastField = null;         // the field the latest event was for, so typing doesn't look it up each key
  var viewAt = Date.now();      // when the page view began,
  var viewAway = 0;             // ... how long it has been hidden since,
  var hiddenAt = 0;             // ... and since when it's hidden now

  function attr(el, name) {
    var v = el && el.getAttribute ? el.getAttribute(name) : null;
    return typeof v === "string" ? v : "";
  }

  // What the collector takes as a form's or a field's key: letters, digits, _ . : - [ ] / # @
  // and inner spaces, up to 64. React's useId ids (":r1:", "«r1»") are new each render, so
  // they name nothing.
  function keyOf(value) {
    if (/^[:«]/.test(value)) return "";
    return String(value).replace(/[^A-Za-z0-9_.:\-\[\]\/#@ ]+/g, "_").replace(/\s+/g, " ").trim().slice(0, 64).trim();
  }

  function trackable(el) {
    var tag = el && el.nodeType === 1 ? el.tagName : "";
    return tag === "SELECT" || tag === "TEXTAREA" || (tag === "INPUT" && !SKIP[String(el.type).toLowerCase()]);
  }

  function kindOf(el) {
    return el.tagName === "INPUT" ? String(el.type || "text").toLowerCase() : el.tagName.toLowerCase();
  }

  // A form's fields in order, each with its key and place.
  function layout(box) {
    var out = [], index = Object.create(null), n = 0;
    each(box.elements || box.querySelectorAll("input, select, textarea"), function (el) {
      if (!trackable(el) || (el.closest && el.closest("[data-private]"))) return;
      var key = keyOf(attr(el, "data-cl-field") || attr(el, "name") || attr(el, "id")) || kindOf(el) + "#" + n;
      if (!(key in index)) index[key] = n++;
      out.push({ el: el, key: key, i: index[key] });
    });
    return out;
  }

  function formKey(box) {
    var key = keyOf(attr(box, "data-cl-form") || attr(box, "id") || attr(box, "name"));
    if (!key && box.tagName === "FORM" && attr(box, "action")) {
      try {
        var to = new URL(attr(box, "action"), loc.href);
        key = keyOf((to.origin === loc.origin ? "" : to.host) + to.pathname);
      } catch (e) { /* named by its place instead */ }
    }
    return key || "form#" + Math.max(0, Array.prototype.indexOf.call(d.forms || [], box));
  }

  // The watched field an event is for: { el, key, i, kind, box, form }, or null.
  function fieldOf(el) {
    if (!FORMS || !counting() || !trackable(el)) return null;
    if (lastField && lastField.el === el) return lastField;
    var box = el.form || (el.closest && el.closest("[data-cl-form]"));
    if (!box || attr(box, "data-cl-form") === "off") return null;
    var hit = null;
    each(layout(box), function (f) { if (f.el === el) hit = f; });
    if (!hit) return null;
    hit.box = box;
    hit.form = formKey(box);
    hit.kind = kindOf(el);
    return (lastField = hit);
  }

  // Random, and tied to nothing: it only lets the collector keep an attempt's later send.
  function rid() {
    var out = "";
    try {
      each(crypto.getRandomValues(new Uint8Array(8)), function (b) { out += (b < 16 ? "0" : "") + b.toString(16); });
    } catch (e) { /* no crypto */ }
    return out.length === 16 ? out : (Math.random().toString(16).slice(2) + Date.now().toString(16)).slice(0, 16);
  }

  function open(box) {
    for (var k = attempts.length - 1; k >= 0; k--) {
      if (attempts[k].box === box && !attempts[k].done) return attempts[k];
    }
    return null;
  }

  function attempt(hit, t) {
    var a = open(hit.box);
    if (!a) {
      a = { box: hit.box, key: hit.form, path: current, id: rid(), start: t, lead: t - viewAt - viewAway,
            away: 0, fields: Object.create(null), order: [], last: "", tries: 0, left: 0, leftAway: 0 };
      attempts.push(a);
    }
    a.dirty = true;
    return a;
  }

  function field(a, hit) {
    var f = a.fields[hit.key];
    if (!f) {
      f = a.fields[hit.key] = { k: hit.kind, i: hit.i, n: 0, ms: 0, fix: 0, bad: 0, at: 0 };
      a.order.push(hit.key);
    }
    f.el = hit.el;
    return f;
  }

  // Into a field: a visit to it, unless it's the same field getting focus back from another
  // tab or app. `late`: its focus came before the visitor did anything (autofocus) or before
  // c.js was watching, so how long they waited to type isn't known.
  function enter(hit, t, late) {
    var a = attempt(hit, t), f = field(a, hit), back = f.paused;
    each(a.order, function (k) { a.fields[k].paused = false; });
    if (!back) {
      if (!f.n && a.left && !late) f.gap = t - a.left - (a.away - a.leftAway);
      f.n++;
    }
    f.at = t;
    f.atAway = a.away;
    f.late = Boolean(late);
    a.last = hit.key;
  }

  function formFocus(e) {
    var hit = fieldOf(e.target);
    // The page focusing a field itself (autofocus) before the visitor has done anything
    // isn't an attempt. Typing in it is, from then.
    if (!hit || (nav.userActivation && !nav.userActivation.hasBeenActive)) return;
    enter(hit, Date.now(), false);
  }

  function formBlur(e) {
    var hit = fieldOf(e.target), a = hit && open(hit.box), f = a && a.fields[hit.key], t = Date.now();
    if (!f || !f.at) return;
    f.ms += t - f.at - (a.away - f.atAway);
    f.at = 0;
    a.left = t;
    a.leftAway = a.away;
    a.dirty = true;
    // The window lost focus, to another tab or app: the field gets it back when they return.
    if (typeof d.hasFocus === "function" && !d.hasFocus()) f.paused = true;
  }

  function formInput(e) {
    var el = e.target, hit = fieldOf(el), t = Date.now();
    if (!hit) return;
    var a = open(hit.box), f = a && a.fields[hit.key];
    if (d.activeElement !== el && (hit.kind === "checkbox" || hit.kind === "radio")) {
      // Safari doesn't focus a checkbox or radio button it clicks: a visit all the same, untimed.
      a = attempt(hit, t);
      f = field(a, hit);
      if (!f.n && a.left) f.gap = t - a.left - (a.away - a.leftAway);
      f.n = f.n || 1;
      a.last = hit.key;
      return;
    }
    if (d.activeElement !== el) {
      // Filled in while the visitor was in another field: the browser's autofill.
      if (!a) return;
      field(a, hit).auto = 1;
      a.dirty = true;
      return;
    }
    if (!f || !f.at) {
      enter(hit, t, true);
      a = open(hit.box);
      f = a.fields[hit.key];
    }
    if (f.wait === undefined && !f.late) f.wait = t - f.at - (a.away - f.atAway);
    var type = e.inputType || "", del = type.indexOf("delete") === 0;
    if (del && !f.del) f.fix++;
    f.del = del;
    if (/^insertFrom(Paste|Drop)/.test(type)) f.paste = 1;
    if (autofilled(el)) f.auto = 1;
    a.last = hit.key;
    a.dirty = true;
  }

  function autofilled(el) {
    try { return el.matches(":autofill"); } catch (e) {
      try { return el.matches(":-webkit-autofill"); } catch (x) { return false; }
    }
  }

  // The browser's validation refusing a field (required, its type, a pattern) as a submit is
  // tried. One try's refusals come together, and count once in `tries`.
  function formInvalid(e) {
    var hit = fieldOf(e.target);
    if (!hit || (nav.userActivation && !nav.userActivation.hasBeenActive)) return;
    var a = attempt(hit, Date.now());
    field(a, hit).bad++;
    if (!a.trying) {
      a.tries++;
      a.trying = true;
      setTimeout(function () { a.trying = false; }, 0);
    }
  }

  function formSubmit(e) {
    var a = open(e.target);
    // A form nobody filled in (a sign-out button) isn't an attempt.
    if (a && a.order.length) finish(a);
  }

  function finish(a) {
    a.done = true;
    return enqueue(attemptItem(a, "submit"));
  }

  // Whether a field has anything in it (a radio group or checkboxes: any of them checked).
  // The one time a value is read, and only this goes.
  function filled(a, key, f) {
    var el = f.el, any = false;
    if (el.type === "checkbox" || el.type === "radio") {
      each(layout(a.box), function (x) { if (x.key === key && x.el.checked) any = true; });
      return any;
    }
    return /\S/.test(el.value || "");
  }

  function cap(ms) {
    return Math.max(0, Math.min(Math.round(ms), 3600000));
  }

  function attemptItem(a, res) {
    var t = Date.now(), away = a.away + (hiddenAt ? t - hiddenAt : 0);
    a.dirty = false;
    var item = { t: "form", p: a.path, a: a.id, f: a.key, res: res, ms: cap(t - a.start - away), fields: [] };
    each(a.order.slice(0, MAX_FIELDS), function (key) {
      var f = a.fields[key], out = { f: key, k: f.k, i: f.i };
      var ms = f.ms + (f.at ? t - f.at - (away - f.atAway) : 0);   // so far, if they're still in it
      if (f.n) out.n = f.n;
      if (ms > 0) out.ms = cap(ms);
      if (f.wait !== undefined) out.wait = cap(f.wait);
      if (f.gap !== undefined) out.gap = cap(f.gap);
      if (f.fix) out.fix = f.fix;
      if (f.bad) out.bad = f.bad;
      if (f.paste) out.paste = 1;
      if (f.auto) out.auto = 1;
      if (filled(a, key, f)) out.fill = 1;
      item.fields.push(out);
    });
    if (a.lead > 0) item.lead = cap(a.lead);
    if (a.tries) item.tries = a.tries;
    if (a.last) item.last = a.last;
    return item;
  }

  // The page view's attempts not submitted, as given up: for good when the page view is
  // over, and for now when the page is only hidden. A visitor who comes back to finish has
  // the attempt sent again, under the same id, and the collector keeps the later send.
  function abandon(over) {
    each(attempts, function (a) {
      if (!a.done && a.dirty) queue.push(attemptItem(a, "abandon"));
    });
    if (over) attempts = [];
  }

  // Back from another tab or app: time away isn't time spent on the page or its forms.
  function back() {
    if (!hiddenAt) return;
    var gone = Date.now() - hiddenAt;
    hiddenAt = 0;
    viewAway += gone;
    each(attempts, function (a) { a.away += gone; });
  }

  // A form sent some way the browser doesn't call a submit: by script (form.submit(), a
  // button that fetches), or fields in a [data-cl-form] box. By element, or by its key.
  function submitted(target) {
    var box = target && target.nodeType === 1 ? target.form || (target.closest && target.closest("form, [data-cl-form]")) : null;
    var a = null;
    each(attempts, function (x) {
      if (!x.done && x.order.length && (x.box === box || x.key === target)) a = x;
    });
    return a ? finish(a) : Promise.resolve();
  }

  function watchForms() {
    if (!FORMS) return;
    each([["focusin", formFocus], ["focusout", formBlur], ["input", formInput], ["invalid", formInvalid],
          ["submit", formSubmit]], function (on) {
      d.addEventListener(on[0], function (e) {
        try { on[1](e); } catch (x) { /* never in the way of the page's own handling */ }
      }, true);
    });
  }

  /* ---------------------------------------------------------------- Google's tracking calls */

  var GA_SKIP = { event: 1, event_callback: 1, event_timeout: 1, eventCallback: 1, eventTimeout: 1,
    send_to: 1, hitCallback: 1 };

  function each(list, fn) {
    Array.prototype.forEach.call(list, function (item) {
      try { fn(item); } catch (e) { /* a page's odd call must never break its own push */ }
    });
  }

  // What the collector takes as a name: letters, digits, _ . : - and inner spaces.
  // "open/close" becomes "open_close"; "Add to cart!" becomes "Add to cart".
  function gaName(value) {
    if (typeof value !== "string" && typeof value !== "number") return "";
    var bad = "[^A-Za-z0-9_.:\\- ]+";
    return String(value).replace(new RegExp("^" + bad + "|" + bad + "$", "g"), "")
      .replace(new RegExp(bad, "g"), "_").replace(/\s+/g, " ").trim().slice(0, 64).trim();
  }

  function gaPath(value) {
    if (typeof value !== "string" || !value) return "";
    try { return new URL(value, loc.href).pathname; } catch (e) { return ""; }
  }

  function gaProps(params) {
    var out = {};
    Object.keys(params || {}).forEach(function (k) {
      if (!GA_SKIP[k] && k.indexOf("gtm.") !== 0) out[k] = params[k];
    });
    return out;
  }

  // An event in GA4's shape, from an old category / action / label / value.
  function gaEvent(category, action, label, value, nonInteraction) {
    var props = {};
    if (category !== undefined && category !== null && category !== "") props.event_category = String(category);
    if (label !== undefined && label !== null && label !== "") props.event_label = String(label);
    if (value !== undefined && value !== null && value !== "" && isFinite(value)) props.value = Number(value);
    if (nonInteraction === true || nonInteraction === 1) props.non_interaction = true;
    var name = gaName(action);
    return name ? track(name, props) : Promise.resolve();
  }

  // Google runs a callback once its hit is sent, and pages often navigate in it. Make it
  // run at most once, in place, so Google (if it is here, or arrives later) shares the
  // guard; run it ourselves when Google's script for that API isn't on the page.
  function gaCallback(holder, key, sent, google, timeout) {
    var fn = holder && holder[key];
    if (typeof fn !== "function") return;
    var called = false;
    var once = function () {
      if (called) return;
      called = true;
      fn.apply(this, arguments);
    };
    holder[key] = once;
    if (google) return;
    setTimeout(once, isFinite(timeout) && timeout > 0 ? Math.min(+timeout, 10000) : 2000);
    sent.then(function () { once(); });
    flush();
  }

  /* gtag.js and Tag Manager: gtag() pushes its `arguments`; GTM pushes { event, ... }. */
  function fromLayer(entry) {
    var google = Boolean(w.google_tag_manager);
    if (Object.prototype.toString.call(entry) === "[object Arguments]") {
      var command = entry[0], params = entry[2];
      if (command === "event" && typeof entry[1] === "string") {
        params = params && typeof params === "object" ? params : {};
        var name = gaName(entry[1]), path = gaPath(params.page_path || params.page_location), sent;
        // A page_view with no page is the one already counted; page() ignores a repeat.
        if (name === "page_view") sent = path ? page(path) : Promise.resolve();
        else sent = name ? track(name, gaProps(params)) : Promise.resolve();
        gaCallback(params, "event_callback", sent, google, params.event_timeout);
      } else if (command === "set" || command === "config") {
        var fields = command === "set" ? (entry[1] === "user_id" ? { user_id: entry[2] } : entry[1]) : params;
        if (fields && typeof fields === "object" && fields.user_id) api.identify(String(fields.user_id));
      }
    } else if (entry && typeof entry === "object" && !Array.isArray(entry) &&
               typeof entry.event === "string" && entry.event.indexOf("gtm.") !== 0 && gaName(entry.event)) {
      gaCallback(entry, "eventCallback", track(gaName(entry.event), gaProps(entry)), google, entry.eventTimeout);
    }
  }

  /* analytics.js: ga("send", "event", category, action, label, value, fields), and friends. */
  var gaState = { page: "" };

  function fromGa(args) {
    var command = args[0];
    if (typeof command !== "string") return;   // ready callbacks are analytics.js's to run
    var method = command.slice(command.lastIndexOf(".") + 1);   // "tracker2.send" -> "send"
    var rest = Array.prototype.slice.call(args, 1);
    var fields = rest.length && rest[rest.length - 1] && typeof rest[rest.length - 1] === "object" ? rest.pop() : null;
    if (method === "set" || method === "create") {
      var set = fields || {};
      if (method === "set" && typeof rest[0] === "string") { set = {}; set[rest[0]] = rest[1]; }
      if (typeof set.page === "string") gaState.page = set.page;
      if (set.userId) api.identify(String(set.userId));
      return;
    }
    if (method !== "send") return;
    var hit = fields || {};
    var type = rest[0] || hit.hitType, sent;
    if (type === "event") {
      sent = gaEvent(rest.length > 1 ? rest[1] : hit.eventCategory, rest.length > 2 ? rest[2] : hit.eventAction,
        rest.length > 3 ? rest[3] : hit.eventLabel, rest.length > 4 ? rest[4] : hit.eventValue, hit.nonInteraction);
    } else if (type === "pageview") {
      var path = gaPath(rest[1] || hit.page || gaState.page);
      sent = path ? page(path) : Promise.resolve();
    } else {
      return;
    }
    if (fields) gaCallback(fields, "hitCallback", sent, Boolean(w.gaplugins), 2000);
  }

  /* ga.js: _gaq.push(["_trackEvent", category, action, label, value, nonInteraction]). */
  var gaqCallback = null;

  function fromGaq(command) {
    if (!Array.isArray(command) || typeof command[0] !== "string") return;
    var method = command[0].slice(command[0].lastIndexOf(".") + 1);   // "t2._trackEvent"
    var sent;
    if (method === "_set" && command[1] === "hitCallback" && typeof command[2] === "function") {
      gaqCallback = command;   // ga.js runs it after the next hit
      return;
    }
    if (method === "_trackEvent") sent = gaEvent(command[1], command[2], command[3], command[4], command[5]);
    else if (method === "_trackPageview") sent = gaPath(command[1]) ? page(gaPath(command[1])) : Promise.resolve();
    else return;
    if (gaqCallback) {
      gaCallback(gaqCallback, 2, sent, Boolean(w._gat), 2000);
      gaqCallback = null;
    }
  }

  // Hear every later push to a queue, after whatever push did already (the array's own,
  // or Google's, when its script has taken the queue over). Entries already in an array
  // queue are handled first.
  function tap(queue, handle) {
    if (!queue || typeof queue.push !== "function") return;
    if (Array.isArray(queue)) each(queue.slice(), handle);
    var push = queue.push;
    queue.push = function () {
      var out = push.apply(queue, arguments);
      each(arguments, handle);
      return out;
    };
  }

  function watchGoogle() {
    if (!GOOGLE) return;
    // gtag.js and GTM. Defining gtag() is what Google's snippet does; a later snippet keeps ours.
    w[LAYER] = w[LAYER] || [];
    tap(w[LAYER], fromLayer);
    if (typeof w.gtag !== "function") w.gtag = function () { w[LAYER].push(arguments); };

    // analytics.js. Its snippet's stub queues into ga.q for analytics.js; keep doing that, so
    // analytics.js still gets every call if it loads, and hear each call on the way through.
    var name = typeof w.GoogleAnalyticsObject === "string" ? w.GoogleAnalyticsObject : "ga";
    var original = typeof w[name] === "function" ? w[name] : null;
    var backlog = original && Array.isArray(original.q) ? original.q.slice() : [];
    var ga = function () {
      try { fromGa(arguments); } catch (e) { /* never break the page's call */ }
      if (original) return original.apply(this, arguments);
      (ga.q = ga.q || []).push(arguments);
    };
    if (original) Object.keys(original).forEach(function (k) { ga[k] = original[k]; });
    else ga.l = Date.now();
    w[name] = ga;
    each(backlog, fromGa);

    // ga.js: an array until ga.js loads and swaps in its own object; either has push().
    w._gaq = w._gaq || [];
    tap(w._gaq, fromGaq);
  }

  /* ---------------------------------------------------------------- errors */

  function report(kind, err, extra) {
    if (!ERRORS || errors >= MAX_ERRORS || excluded(loc.pathname)) return Promise.resolve();
    extra = extra || {};
    var message = extra.m || (err && err.message) || (typeof err === "string" ? err : "") ||
      (err !== undefined ? String(err) : "Unknown error");
    var item = {
      t: "err", k: kind, m: String(message).slice(0, 500),
      name: (err && err.name) || extra.name || "",
      st: err && err.stack ? String(err.stack).slice(0, 4000) : "",
      src: extra.src || "", ln: extra.ln || 0, col: extra.col || 0
    };
    if (extra.context) item.st = (item.st ? item.st + "\n" : "") + "context: " + JSON.stringify(extra.context).slice(0, 500);
    var key = kind + "|" + item.m + "|" + item.st.slice(0, 300);
    if (seen[key]) return Promise.resolve();
    seen[key] = 1;
    errors++;
    return enqueue(item);
  }

  function watchErrors() {
    if (!ERRORS) return;
    w.addEventListener("error", function (e) {
      var t = e.target;
      if (t && t !== w && t.nodeType === 1) {
        var url = t.currentSrc || t.src || t.href || "";
        report("resource", null, { m: "Failed to load <" + t.tagName.toLowerCase() + "> " + url, src: url });
        return;
      }
      report("error", e.error, { m: e.message, src: e.filename, ln: e.lineno, col: e.colno });
    }, true);
    w.addEventListener("unhandledrejection", function (e) {
      var r = e.reason;
      report("rejection", r instanceof Error ? r : null,
        { m: r instanceof Error ? r.message : "Unhandled rejection: " + safe(r) });
    });
    d.addEventListener("securitypolicyviolation", function (e) {
      report("csp", null, { m: "CSP " + e.violatedDirective + " blocked " + (e.blockedURI || "inline"),
        src: e.sourceFile, ln: e.lineNumber, col: e.columnNumber });
    });
  }

  function safe(value) {
    try { return typeof value === "string" ? value : JSON.stringify(value); } catch (e) { return String(value); }
  }

  /* ---------------------------------------------------------------- what the collector makes of this visitor */

  var asked = null;

  // A bot (and whether a crawler is the real one), the kind of connection (VPN, Tor,
  // iCloud Private Relay, a datacenter), the lists that name the address. Asked only
  // when the page wants it, once a page; nothing is stored or counted, so it works with
  // Do Not Track too. null when the collector can't be reached, and asked again next time.
  function who(callback) {
    if (!asked) {
      var query = [ZONE ? "tz=" + encodeURIComponent(ZONE) : "", nav.webdriver ? "wd=1" : "",
        DRIVER ? "auto=" + encodeURIComponent(DRIVER) : ""].filter(Boolean).join("&");
      asked = fetch(BASE + "/who" + (query ? "?" + query : ""), { credentials: "omit", cache: "no-store" }).then(function (res) {
        if (!res.ok) throw new Error("who: " + res.status);
        return res.json();
      }).then(null, function () {
        asked = null;
        return null;
      });
    }
    if (typeof callback === "function") asked.then(callback);
    return asked;
  }

  /* ---------------------------------------------------------------- identity (consent only) */

  function today() { return new Date().toISOString().slice(0, 10); }

  function fingerprint() {
    var canvas = d.createElement("canvas");
    var ctx = canvas.getContext("2d");
    if (ctx) {
      ctx.textBaseline = "top";
      ctx.font = "14px Arial";
      ctx.fillText(loc.host + " \u{1F9ED} fp", 2, 2);
    }
    var gl = d.createElement("canvas").getContext("webgl");
    var debug = gl && gl.getExtension("WEBGL_debug_renderer_info");
    var parts = [
      nav.userAgent, (nav.languages || []).join(","), Intl.DateTimeFormat().resolvedOptions().timeZone,
      screen.width + "x" + screen.height + "x" + screen.colorDepth + "@" + w.devicePixelRatio,
      nav.hardwareConcurrency, nav.deviceMemory, canvas.toDataURL(),
      debug ? gl.getParameter(debug.UNMASKED_RENDERER_WEBGL) : ""
    ];
    return crypto.subtle.digest("SHA-256", new TextEncoder().encode(parts.join("|"))).then(function (h) {
      return Array.prototype.map.call(new Uint8Array(h).slice(0, 16), function (b) {
        return b.toString(16).padStart(2, "0");
      }).join("");
    });
  }

  function identify() {
    if (ident || OPTED_OUT || !w.crypto || !crypto.randomUUID) return false;
    var vid = cookie("vid");
    if (!vid || !UUID.test(vid)) vid = crypto.randomUUID();
    // Set again on every visit, so the 400-day clock keeps restarting.
    cookie("vid", vid, 60 * 60 * 24 * 400);
    var sid = session("cl_sid"), sd = session("cl_sd"), fresh = false;
    if (!sid || !UUID.test(sid) || !/^\d{4}-\d{2}-\d{2}$/.test(sd || "") || idle()) {
      sid = crypto.randomUUID();
      sd = today();
      fresh = true;
      session("cl_sid", sid);
      session("cl_sd", sd);
      session("cl_seq", "0");
      session("cl_at", String(Date.now()));
    }
    var uid = wanted !== null ? wanted : session("cl_uid") || "";
    session("cl_uid", uid || null);
    ident = { vid: vid, sid: sid, sd: sd, fresh: fresh, uid: uid };
    if (crypto.subtle) fingerprint().then(function (fp) { ident && (ident.fp = fp); }, function () {});
    listen();
    if (!fresh) claim();
    return true;
  }

  // `cl_at` is the session's latest page view or event. None (a session from before
  // sessions ended) counts as long ago.
  function idle() {
    var at = parseInt(session("cl_at"), 10);
    return !(at > 0) || Date.now() - at > IDLE;
  }

  // A page view or an event, which keeps a session going; after IDLE without one, the
  // tab starts another. The first item of a session says so (`new`).
  function interaction(item) {
    if (!ident) return item;
    if (session("cl_at") !== null && idle()) rotate();
    session("cl_at", String(Date.now()));
    if (ident.fresh) {
      item.new = true;
      ident.fresh = false;
    }
    return item;
  }

  function rotate() {
    flush();   // what's queued, and recorded, is the old session's
    ship();
    ident.sid = crypto.randomUUID();
    ident.sd = today();
    ident.fresh = true;
    session("cl_sid", ident.sid);
    session("cl_sd", ident.sd);
    session("cl_seq", "0");
    // The recording goes on under the new session, from a fresh snapshot of the page.
    snapshot();
  }

  /* ---------------------------------------------------------------- one session a tab */

  // A duplicated tab, or a window opened with an opener, starts with a copy of this tab's
  // sessionStorage: its session, and its count of recording chunks, so the two would write
  // each other's chunks and play as one recording. So a page that picks a session up from
  // storage asks the site's other tabs (a BroadcastChannel, consented visitors only)
  // whether one of them has it, twice, CLAIM_MS apart. The page this tab is leaving may
  // still answer the first time; it stops answering as it goes (`leaving`), and is gone by
  // the second. A tab that still answers is the one this was copied from, and this tab
  // takes a session of its own.
  var TAB = Math.random().toString(36).slice(2) + Date.now().toString(36);
  var CLAIM_MS = 200;
  var tabs = null;
  var heard = 0;                // answers to this page's latest claim
  var leaving = false;

  function listen() {
    if (tabs || typeof w.BroadcastChannel !== "function") return;
    try { tabs = new w.BroadcastChannel("carterlitics"); } catch (e) { return; }
    tabs.onmessage = function (e) {
      var m = e && e.data;
      if (!m || !ident || leaving || m.from === TAB || m.sid !== ident.sid) return;
      if (m.t === "claim") tabs.postMessage({ t: "mine", sid: m.sid, to: m.from, from: TAB });
      else if (m.t === "mine" && m.to === TAB) heard++;
    };
  }

  // Closed as the page goes (and while it's in the back/forward cache), so it answers no
  // one and doesn't keep the page from being cached.
  function quiet() {
    if (tabs) {
      try { tabs.close(); } catch (e) { /* closed already */ }
    }
    tabs = null;
  }

  function claim() {
    if (!tabs || !ident) return;
    var sid = ident.sid;
    function ask(then) {
      if (!tabs) return;
      heard = 0;
      tabs.postMessage({ t: "claim", sid: sid, from: TAB });
      setTimeout(function () {
        if (heard && ident && ident.sid === sid) then();
      }, CLAIM_MS);
    }
    ask(function () { ask(fork); });
  }

  // This tab's copy of another tab's session becomes a session of its own, from here on.
  // What's still queued goes out under it, its first page view or event saying it's new,
  // and a recording that has already sent chunks under the old one starts again from a
  // fresh snapshot (otherwise its first chunk still holds the page's).
  function fork() {
    ident.sid = crypto.randomUUID();
    ident.sd = today();
    session("cl_sid", ident.sid);
    session("cl_sd", ident.sd);
    session("cl_seq", "0");
    session("cl_at", String(Date.now()));
    var first = queue.filter(function (i) { return i.t === "pv" || i.t === "ev"; })[0];
    ident.fresh = !first;
    if (first) {
      first.new = true;
      clearTimeout(timer);
      timer = setTimeout(flush, 0);
    }
    if (recorder && shipped) {
      recorder.buffer = [];
      snapshot();
    }
  }

  function forget() {
    quiet();
    cookie("vid", "", 0);
    ["cl_sid", "cl_sd", "cl_seq", "cl_uid", "cl_at"].forEach(function (k) { session(k, null); });
    ident = null;
  }

  /* ---------------------------------------------------------------- recording (consent only) */

  function load(src) {
    return new Promise(function (resolve, reject) {
      var s = d.createElement("script");
      s.src = src;
      s.async = true;
      s.onload = resolve;
      s.onerror = reject;
      d.head.appendChild(s);
    });
  }

  function record() {
    if (!RECORD || recorder || !ident || excluded(loc.pathname)) return;
    recorder = { buffer: [] };
    load(RRWEB).then(function () {
      if (!ident || !w.rrweb) return;
      recorder.stop = w.rrweb.record({
        emit: function (event) { recorder.buffer.push(event); },
        maskAllInputs: true,
        blockSelector: "[data-private]",
        maskTextSelector: "[data-mask]",
        sampling: { mousemove: 50, scroll: 150, input: "last" }
      });
      recorder.timer = setInterval(ship, 5000);
    }, function () { recorder = null; });
  }

  // A fresh snapshot of the whole page for the replay to go on from: under a new session,
  // or for a page the back/forward cache brought back, whose changes would otherwise land
  // on the snapshot of the page before it.
  function snapshot() {
    if (recorder && recorder.stop && w.rrweb && w.rrweb.record.takeFullSnapshot) {
      try { w.rrweb.record.takeFullSnapshot(true); } catch (e) { /* the next page load starts one */ }
    }
  }

  // A page in the recording, for the replay's timeline. A page load's snapshot already says
  // where it is (rrweb's Meta event); this marks the rest: an SPA's navigation, a page
  // brought back by Back.
  function mark(path) {
    if (recorder && recorder.stop && w.rrweb && w.rrweb.record.addCustomEvent) {
      try { w.rrweb.record.addCustomEvent("page", { path: path }); } catch (e) { /* not recording yet */ }
    }
  }

  function nextSeq() {
    var seq = parseInt(session("cl_seq"), 10) || 0;
    session("cl_seq", String(seq + 1));
    return seq;
  }

  function ship(final) {
    if (!recorder || !recorder.buffer.length || !ident) return;
    var events = recorder.buffer;
    recorder.buffer = [];
    // Hidden means the page may be going away: only beacon-sized chunks still get out.
    var limit = final || d.visibilityState === "hidden" ? BEACON_BYTES : CHUNK_BYTES;
    var batch = [], size = 0;
    function send() {
      if (!batch.length) return;
      var seq = nextSeq();
      if (seq < MAX_CHUNKS) {
        post("/r", { sid: ident.sid, vid: ident.vid, sd: ident.sd, seq: seq, events: batch, p: loc.pathname }, final);
        shipped++;
      }
      batch = [];
      size = 0;
    }
    events.forEach(function (event) {
      var bytes = JSON.stringify(event).length;
      if (bytes > CHUNK_BYTES) return;
      if (size + bytes > limit) send();
      batch.push(event);
      size += bytes;
    });
    send();
  }

  /* ---------------------------------------------------------------- consent */

  var banner = d.querySelector("[data-consent]");

  // The cookie holds the choice and, past version 1, the version it was made under: "all.2".
  function stored() {
    var m = /^(all|essential)(?:\.(\d{1,6}))?$/.exec(cookie("consent") || "");
    return m ? { value: m[1], version: m[2] ? parseInt(m[2], 10) : 1 } : null;
  }

  function choice() {
    var got = stored();
    if (!got || (got.value === "all" && got.version < CONSENT_VERSION)) return null;
    return got.value;
  }

  // The version of the Accept the visitor gave, kept with what they send (0: none, as in full mode).
  function accepted() {
    var got = stored();
    return got && got.value === "all" ? got.version : 0;
  }

  function apply(value) {
    if (value === "all" && MODE !== "anonymous" && identify()) {
      // Consent given on this page: its view was already counted, so log it once more
      // for the session only (`dup`), then start recording.
      if (current !== null && counting()) {
        enqueue(interaction({ t: "pv", p: current, r: d.referrer || null, dup: true }));
      }
      record();
    } else if (value !== "all") {
      forget();
    }
  }

  function consent(value) {
    if (value === undefined) return MODE === "full" ? "all" : choice();
    if (value !== "all" && value !== "essential") return choice();
    var wasRecording = Boolean(recorder);
    cookie("consent", CONSENT_VERSION > 1 ? value + "." + CONSENT_VERSION : value, 60 * 60 * 24 * 365);
    if (banner) banner.hidden = true;
    // A recorder that is already running can't be unloaded; a fresh page stops it.
    if (value === "essential" && wasRecording) {
      forget();
      flush(true).then(function () { loc.reload(); });
      return value;
    }
    apply(value);
    return value;
  }

  function ask() {
    if (!banner || OPTED_OUT || MODE !== "consent") return;
    banner.hidden = false;
    var first = banner.querySelector("button[data-choice]");
    if (first) first.focus({ preventScroll: true });
  }

  function watchConsent() {
    if (banner) {
      banner.addEventListener("click", function (e) {
        var button = e.target.closest && e.target.closest("button[data-choice]");
        if (button) consent(button.dataset.choice);
      });
    }
    d.addEventListener("click", function (e) {
      if (e.target.closest && e.target.closest("[data-consent-reopen]")) ask();
    });
  }

  /* ---------------------------------------------------------------- hello */

  // For whoever opens DevTools: once a page load, what's counted about them here, in a
  // friendly voice, and how to see the rest. The only thing c.js ever prints. A site may
  // greet them too (cartercole.net has a cat, a wave and a cheat code, in pink, periwinkle
  // and cyan), so this one keeps to the counter, in its own words and in green.
  function hello() {
    if (opts.hello === "off" || excluded(loc.pathname) || !w.console || typeof console.log !== "function") return;
    var about = OPTED_OUT
      ? "you've asked not to be tracked, so you aren't. only errors go, anonymously, so things get fixed."
      : ident
        ? "this tab has a session" + (recorder ? " and a replay" : "") +
          (MODE === "consent" ? ", since you said yes. changed your mind? cl(\"consent\", \"essential\")" : ".")
        : "no cookies, no ids, nothing that follows you around.";
    try {
      console.log("%c(\uFF61\u2022\u1D17-)\u2727 a wee page counter lives here!%c\n" +
        "carterlitics counts this site's visits, first-party: " + about + "\n" +
        "ask what it makes of you: await cl(\"who\")",
        "font: 600 14px/1.8 system-ui, sans-serif; color: #22a06b",
        "font: 12px/1.6 system-ui, sans-serif");
    } catch (e) { /* a console that won't */ }
  }

  /* ---------------------------------------------------------------- start */

  var api = {
    v: 1,
    track: track,
    page: page,
    error: function (err, context) { return report("manual", err, { context: context }); },
    consent: consent,
    // "" or null forgets it (signing out). Before consent it waits, in memory, and applies
    // only if the visitor accepts on this page.
    identify: function (uid) {
      if (uid !== null && typeof uid !== "string") return false;
      wanted = uid ? uid.slice(0, 120) : "";
      if (!ident) return false;
      ident.uid = wanted;
      session("cl_uid", wanted || null);
      return true;
    },
    who: who,
    submit: submitted,
    flush: function () { return flush(false); }
  };
  w.carterlitics = api;

  // A page the browser prerenders (speculation rules, or Chrome guessing from what's typed
  // in the address bar) runs its scripts before anyone sees it, and may never be shown.
  // Until it is, c.js is as good as not loaded: cl() calls wait in the stub's queue,
  // Google's in theirs, and nothing is sent. Once it's shown, c.js starts as if it had
  // just loaded, and the page view counts from then.
  var started = false;

  function start() {
    if (started || d.prerendering) return;
    started = true;

    var pending = (w.cl && w.cl.q) || [];
    w.cl = function (command) {
      var fn = api[command];
      return fn ? fn.apply(api, Array.prototype.slice.call(arguments, 1)) : undefined;
    };

    watchErrors();
    watchConsent();
    watchClicks();
    watchForms();

    if (MODE === "full") apply("all");
    else if (MODE === "consent" && OPTED_OUT) forget();
    else if (MODE === "consent" && choice()) apply(choice());
    else if (MODE === "consent") ask();

    page();
    watchNavigation();
    watchLeaving();
    pending.forEach(function (args) { w.cl.apply(null, args); });
    watchGoogle();
    hello();

    d.addEventListener("visibilitychange", function () {
      if (d.visibilityState === "hidden") {
        hiddenAt = hiddenAt || Date.now();
        settle(false);
        ship(true);
        flush(true);
      } else {
        visibleSince = Date.now();
        back();
      }
    });
    w.addEventListener("pagehide", function () {
      leaving = true;
      quiet();
      settle(true);
      ship(true);
      flush(true);
    });
    // Back or forward to a page the browser kept whole (the back/forward cache). It comes
    // back as it was left, without running its scripts again, so this is its page view:
    // counted as the same Back would be if the page loaded anew, from its own referrer.
    // pagehide has already settled the one before. A recording goes on from a fresh
    // snapshot, since the replay last saw the page Back came from.
    w.addEventListener("pageshow", function (e) {
      if (!e || !e.persisted) return;
      snapshot();
      current = null;
      page();
      // Another tab may have been copied from this one while it was cached.
      if (ident) {
        listen();
        claim();
      }
    });
  }

  if (d.prerendering) d.addEventListener("prerenderingchange", start);
  start();
})();
