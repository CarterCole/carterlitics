/* carterlitics — the page's half of first-party analytics. https://github.com/CarterCole/carterlitics

   <script async src="/_c/c.js"></script>

   Always, with no cookie and nothing stored on the device: page views (and SPA
   navigations), engaged time per page, custom events, and JavaScript errors.

   Only after the visitor accepts (the consent banner, or cl("consent", "all")): a
   visitor cookie, a session id, a device fingerprint, and a session recording (rrweb,
   typed text masked, [data-private] blanked, [data-mask] text masked).

   Do Not Track and Global Privacy Control are a standing "no": nothing is identified
   or recorded, page views and events aren't sent, and the banner never shows. Error
   reports still go, anonymously, so the site can be fixed.

   Options, as attributes on the script tag:
     data-mode="consent"   consent (default) · full (identify without asking) · anonymous
     data-record="off"     no session recording even with consent
     data-errors="off"     don't report errors
     data-exclude="/admin,/auth"   path prefixes to leave alone entirely
     data-google="off"     ignore Google Analytics calls (see below)
     data-datalayer="dataLayer"    the name gtag.js / GTM were given for their queue

   API, safe to call before the script loads with this stub in the page:
     <script>window.cl=window.cl||function(){(cl.q=cl.q||[]).push(arguments)}</script>

     cl("track", "signup", { plan: "pro" })   // a Promise once loaded: resolves when sent
     cl("error", err, { where: "checkout" })   // report a caught error
     cl("page")                                 // count a page view by hand
     cl("consent", "all" | "essential")         // what the banner buttons do
     cl("identify", "user-123")                 // tie a consented visitor to your user id
     window.carterlitics.flush()                // send what's queued now

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
  var LAYER = /^[A-Za-z_$][\w$]*$/.test(opts.datalayer || "") ? opts.datalayer : "dataLayer";
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

  var queue = [];
  var waiting = [];
  var timer = null;
  var ident = null;             // { vid, sid, sd, fp, uid, fresh } once consented
  var current = null;           // the path being viewed
  var lastUrl = d.referrer || "";
  var visibleSince = d.visibilityState === "visible" ? Date.now() : 0;
  var engaged = 0;
  var errors = 0;
  var seen = {};
  var recorder = null;

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
    if (ident) {
      e.vid = ident.vid;
      e.sid = ident.sid;
      e.sd = ident.sd;
      if (ident.fp) e.fp = ident.fp;
      if (ident.uid) e.uid = ident.uid;
    }
    return e;
  }

  function flush(final) {
    clearTimeout(timer);
    timer = null;
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
    if (!timer) timer = setTimeout(flush, 1000);
    return new Promise(function (resolve) { waiting.push(resolve); });
  }

  /* ---------------------------------------------------------------- page views */

  function counting() {
    return !OPTED_OUT && current !== null && !excluded(current);
  }

  function settle() {
    // Engaged time: only while the page is visible.
    if (visibleSince) {
      engaged += Date.now() - visibleSince;
      visibleSince = d.visibilityState === "visible" ? Date.now() : 0;
    }
    if (counting() && engaged >= 1000) {
      queue.push({ t: "leave", p: current, ms: Math.min(Math.round(engaged), 3600000) });
    }
    engaged = 0;
  }

  function page(path) {
    path = typeof path === "string" ? path : loc.pathname;
    if (path === current) return Promise.resolve();
    if (current !== null) settle();
    var referrer = current === null ? d.referrer : lastUrl;
    current = path;
    lastUrl = loc.href;
    visibleSince = d.visibilityState === "visible" ? Date.now() : 0;
    if (!counting()) return Promise.resolve();
    var item = { t: "pv", p: path, r: referrer || null };
    if (ident && ident.fresh) {
      item.new = true;
      ident.fresh = false;
    }
    return enqueue(item);
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
    return enqueue({ t: "ev", n: name.slice(0, 64), props: clean });
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
    if (!sid || !UUID.test(sid) || !/^\d{4}-\d{2}-\d{2}$/.test(sd || "")) {
      sid = crypto.randomUUID();
      sd = today();
      fresh = true;
      session("cl_sid", sid);
      session("cl_sd", sd);
      session("cl_seq", "0");
    }
    ident = { vid: vid, sid: sid, sd: sd, fresh: fresh, uid: session("cl_uid") || "" };
    if (crypto.subtle) fingerprint().then(function (fp) { ident && (ident.fp = fp); }, function () {});
    return true;
  }

  function forget() {
    cookie("vid", "", 0);
    ["cl_sid", "cl_sd", "cl_seq", "cl_uid"].forEach(function (k) { session(k, null); });
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

  function choice() {
    var value = cookie("consent");
    return value === "all" || value === "essential" ? value : null;
  }

  function apply(value) {
    if (value === "all" && MODE !== "anonymous" && identify()) {
      // Consent given on this page: its view was already counted, so log it once more
      // for the session only (`dup`), then start recording.
      if (current !== null && counting()) {
        var item = { t: "pv", p: current, r: d.referrer || null, dup: true };
        if (ident.fresh) {
          item.new = true;
          ident.fresh = false;
        }
        enqueue(item);
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
    cookie("consent", value, 60 * 60 * 24 * 365);
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

  /* ---------------------------------------------------------------- start */

  var api = {
    v: 1,
    track: track,
    page: page,
    error: function (err, context) { return report("manual", err, { context: context }); },
    consent: consent,
    identify: function (uid) {
      if (!ident || typeof uid !== "string") return false;
      ident.uid = uid.slice(0, 120);
      session("cl_uid", ident.uid);
      return true;
    },
    flush: function () { return flush(false); }
  };
  w.carterlitics = api;

  var pending = (w.cl && w.cl.q) || [];
  w.cl = function (command) {
    var fn = api[command];
    return fn ? fn.apply(api, Array.prototype.slice.call(arguments, 1)) : undefined;
  };

  watchErrors();
  watchConsent();

  if (MODE === "full") apply("all");
  else if (MODE === "consent" && OPTED_OUT) forget();
  else if (MODE === "consent" && choice()) apply(choice());
  else if (MODE === "consent") ask();

  page();
  watchNavigation();
  pending.forEach(function (args) { w.cl.apply(null, args); });
  watchGoogle();

  d.addEventListener("visibilitychange", function () {
    if (d.visibilityState === "hidden") {
      settle();
      ship(true);
      flush(true);
    } else {
      visibleSince = Date.now();
    }
  });
  w.addEventListener("pagehide", function () {
    settle();
    ship(true);
    flush(true);
  });
})();
