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

   API, safe to call before the script loads with this stub in the page:
     <script>window.cl=window.cl||function(){(cl.q=cl.q||[]).push(arguments)}</script>

     cl("track", "signup", { plan: "pro" })   // a Promise once loaded: resolves when sent
     cl("error", err, { where: "checkout" })   // report a caught error
     cl("page")                                 // count a page view by hand
     cl("consent", "all" | "essential")         // what the banner buttons do
     cl("identify", "user-123")                 // tie a consented visitor to your user id
     window.carterlitics.flush()                // send what's queued now
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

  function envelope(items) {
    var e = { v: 1, w: w.innerWidth, items: items };
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
      Object.keys(props).slice(0, 30).forEach(function (k) {
        var v = props[k];
        if (v === null || typeof v === "number" || typeof v === "boolean") clean[k] = v;
        else clean[k] = String(v).slice(0, 300);
      });
    }
    return enqueue({ t: "ev", n: name.slice(0, 64), props: clean });
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
