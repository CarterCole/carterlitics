/* carterlitics — session recordings, played back with rrweb-player.

     replay?site=…&id=<session>[&vid=<visitor>]   one session; with its visitor, its numbers too
     replay?site=…&visitor=<visitor>              every recorded session of one visitor, in
                                                  turn, after what their sessions add up to

   The events come from /_c/api/recording and the numbers from /_c/api/visitor, which
   answer only to an administrator. A visitor's sessions are stitched together here, in
   the browser, one recording after another with a short gap between them, so no one
   response has to carry them all. The player rebuilds the recorded page in an iframe
   without scripts, and the page's Content-Security-Policy keeps a recorded page from
   loading anything but the sites' own files. Recorded strings reach the page only through
   textContent. */
(function () {
  "use strict";

  var root = document.querySelector("[data-replay]");
  if (!root) return;
  var MOUNT = root.dataset.mount || "/_c";
  var ID = /^[0-9a-fA-F-]{8,64}$/;
  var GAP = 2000;              // between one session and the next, played back to back
  var MAX_SESSIONS = 20;       // a visitor's latest recorded sessions, at most
  var q = new URLSearchParams(location.search);
  var site = q.get("site") || "";
  var visitor = q.get("visitor") || "";
  var id = visitor ? "" : q.get("id") || "";
  var vid = visitor || q.get("vid") || "";
  var nf = new Intl.NumberFormat();

  function $(selector) { return root.querySelector(selector); }

  function el(tag, attrs) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      var v = attrs[k];
      if (v === null || v === undefined) return;
      if (k === "text") node.textContent = v;
      else if (k === "class") node.className = v;
      else node.setAttribute(k, v);
    });
    for (var i = 2; i < arguments.length; i++) add(node, arguments[i]);
    return node;
  }

  function add(node, child) {
    if (child === null || child === undefined || child === false) return;
    if (Array.isArray(child)) child.forEach(function (c) { add(node, c); });
    else node.append(child instanceof Node ? child : String(child));
  }

  function status(text, bad) {
    var s = $("[data-status]");
    s.textContent = text;
    s.hidden = !text;
    s.classList.toggle("status--bad", Boolean(bad));
  }

  function num(n) { return nf.format(n || 0); }
  function stamp(iso) { return (iso || "").slice(0, 16).replace("T", " "); }
  function utc(ms) { return new Date(ms).toISOString().slice(0, 19).replace("T", " "); }

  function dur(ms) {
    if (!ms || ms < 0) return "—";
    var s = Math.round(ms / 1000);
    if (s < 60) return s + "s";
    var m = Math.floor(s / 60);
    if (m < 60) return m + "m " + (s % 60) + "s";
    return Math.floor(m / 60) + "h " + (m % 60) + "m";
  }

  function span(a, b) {
    var t = Date.parse(b) - Date.parse(a);
    return isFinite(t) && t > 0 ? dur(t) : "—";
  }

  function clock(ms) {
    var s = Math.max(0, Math.round(ms / 1000));
    return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
  }

  function link(query, text, title) {
    return el("a", { href: MOUNT + "/admin/replay?" + new URLSearchParams(Object.assign({ site: site }, query)),
                     text: text, title: title || null });
  }

  /* ---------------------------------------------------------------- data */

  // null for "not kept": an unknown visitor, or a recording past its days.
  function api(path, query) {
    var url = MOUNT + "/api" + path + "?" + new URLSearchParams(Object.assign({ site: site }, query));
    return fetch(url, { credentials: "same-origin", cache: "no-store" }).then(function (res) {
      if (res.status === 401) {
        location.replace(MOUNT + "/auth/login?next=" + encodeURIComponent(location.pathname + location.search));
        return new Promise(function () {});
      }
      if (res.status === 404) return null;
      if (!res.ok) throw new Error("Couldn't load it (" + res.status + ").");
      return res.json();
    });
  }

  /* ---------------------------------------------------------------- numbers */

  function tiles(list) {
    return el("div", { class: "tiles" }, list.map(function (t) {
      return el("div", { class: "tile" },
        el("p", { class: "tile-label", text: t[0] }),
        el("p", { class: "tile-value", text: t[1] }),
        t[2] ? el("p", { class: "tile-note" }, t[2]) : null);
    }));
  }

  // What a visitor's sessions add up to.
  function visitorTiles(v) {
    var t = v.totals, n = t.sessions || 1;
    return tiles([
      ["Sessions", num(t.sessions), t.recorded + " recorded · " + num(t.days) + (t.days === 1 ? " day" : " days")],
      ["Pages visited", num(t.unique_pages), num(t.pages) + " page views · " + (t.pages / n).toFixed(1) + " a session"],
      ["Engaged time", dur(t.ms), dur(t.ms / n) + " a session"],
      ["Events", num(t.events), t.errors ? num(t.errors) + (t.errors === 1 ? " error" : " errors") : "no errors"],
      ["First seen", (t.first || "").slice(0, 10), stamp(t.first).slice(11) + " UTC · last seen " + stamp(t.last)]
    ]);
  }

  // One session's numbers, and its visitor's at a glance.
  function sessionTiles(s, v) {
    var t = v.totals, length = span(s.started, s.last);
    return tiles([
      ["Pages", num(s.pages), "landed on " + (s.path || "—") + (s.exit && s.exit !== s.path ? ", left from " + s.exit : "")],
      ["Engaged time", dur(s.ms), length !== "—" ? "of " + length + " in all" : null],
      ["Events", num(s.events), s.errors ? num(s.errors) + (s.errors === 1 ? " error" : " errors") : "no errors"],
      ["From", s.src || "Direct", [s.ch, s.cmp].filter(Boolean).join(" · ") || null],
      ["This visitor", num(t.sessions) + (t.sessions === 1 ? " session" : " sessions"),
        [num(t.unique_pages) + " pages visited · ",
         link({ visitor: v.vid }, t.sessions > 1 ? "see them all" : "see it", "Every session of this visitor")]]
    ]);
  }

  function sessionsTable(v, playing) {
    return el("div", { class: "table-wrap" }, el("table", { class: "log" },
      el("thead", null, el("tr", null, ["Started (UTC)", "Length", "Pages", "Engaged", "Events", "Errors",
        "Landed on", "Left from", "From", "Device", ""].map(function (h, i) {
        return el("th", { class: i >= 2 && i <= 5 ? "num" : null, text: h });
      }))),
      el("tbody", null, v.sessions.map(function (s) {
        return el("tr", null,
          el("td", null, el("span", { class: "when", text: stamp(s.started) })),
          el("td", { text: span(s.started, s.last) }),
          el("td", { class: "num", text: num(s.pages) }),
          el("td", { class: "num", text: dur(s.ms) }),
          el("td", { class: "num", text: num(s.events) }),
          el("td", { class: "num" + (s.errors ? " bad" : ""), text: num(s.errors) }),
          el("td", null, el("span", { class: "clip", text: s.path || "", title: s.path || "" })),
          el("td", null, el("span", { class: "clip", text: s.exit || "", title: s.exit || "" })),
          el("td", null, el("span", { class: "clip", text: [s.src, s.cmp].filter(Boolean).join(" · "),
                                      title: s.ch || "" })),
          el("td", null, el("span", { class: "clip", text: [s.browser, s.os, s.device].filter(Boolean).join(" · ") })),
          el("td", null, !s.rec ? el("span", { class: "empty", text: "no recording" })
            : playing[s.id] ? el("span", { class: "empty", text: "above" })
            : link({ id: s.id, vid: v.vid }, "Replay")));
      }))));
  }

  function viewedTable(v) {
    if (!v.pages.length) return el("p", { class: "empty", text: "No page views logged." });
    var most = v.pages[0].views || 1;
    return el("table", { class: "ranked" },
      el("thead", null, el("tr", null, el("th", { text: "Page" }), el("td", { class: "num", text: "Views" }))),
      el("tbody", null, v.pages.map(function (p) {
        return el("tr", null,
          el("th", { scope: "row" }, el("span", { class: "name", style: "--w:" + (p.views / most * 100) + "%",
                                                  text: p.path, title: p.path })),
          el("td", { class: "num num--lead", text: num(p.views) }));
      })));
  }

  function show(selector, body) {
    var box = $(selector);
    box.replaceChildren(body);
    box.closest("[hidden]") && (box.closest("[hidden]").hidden = false);
    box.hidden = false;
  }

  /* ---------------------------------------------------------------- pages and stitching */

  // One session's events after another's, each moved to begin GAP after the one before
  // ended: a visitor's sessions can be days apart, or overlap (two tabs at once).
  function stitch(parts) {
    var events = [], starts = [], shifts = [], end = null;
    parts.forEach(function (part) {
      var first = part.events[0].timestamp;
      var shift = end === null ? 0 : end + GAP - first;
      starts.push(events.length);
      shifts.push(shift);
      part.events.forEach(function (e) {
        events.push(shift ? Object.assign({}, e, { timestamp: e.timestamp + shift }) : e);
      });
      end = part.events[part.events.length - 1].timestamp + shift;
    });
    return { events: events, starts: starts, shifts: shifts };
  }

  // The pages a recording went through. Each page load begins with rrweb's Meta event (its
  // address) and its snapshot; c.js marks the rest with a "page" event (an SPA's
  // navigation, a page Back brought back). The same page twice in a row is one stop: a
  // reload, or a new session's fresh snapshot, unless a stitched session begins there.
  // `index` is the event the stop follows, `real` when it happened.
  function pagesOf(events, starts, shifts) {
    var out = [], session = -1, last = null, next = 0;
    events.forEach(function (e, i) {
      if (next < starts.length && i === starts[next]) {
        session = next++;
        last = null;
      }
      var path = "";
      if (e.type === 4 && e.data && typeof e.data.href === "string") {
        try { path = new URL(e.data.href).pathname; } catch (x) { return; }
        if (events[i + 1] && events[i + 1].type === 2) i += 1;
      } else if (e.type === 5 && e.data && e.data.tag === "page" && e.data.payload) {
        path = e.data.payload.path;
      }
      if (typeof path !== "string" || path.charAt(0) !== "/") return;
      path = path.slice(0, 200);
      if (path === last) return;
      out.push({ path: path, at: e.timestamp, real: e.timestamp - shifts[session], index: i, session: session,
                 opens: last === null });
      last = path;
    });
    return out;
  }

  // The player marks every custom event on its timeline, titled with its tag: so each stop
  // gets one, tagged with its path, after the event it follows, in place of c.js's own
  // `page` events (which change nothing on the page).
  function withMarkers(events, pages, tags, color) {
    var after = {};
    pages.forEach(function (p) {
      after[p.index] = p;
      tags[p.path] = color;
    });
    var out = [];
    events.forEach(function (e, i) {
      if (!(e.type === 5 && e.data && e.data.tag === "page")) out.push(e);
      if (after[i]) out.push({ type: 5, timestamp: after[i].at, data: { tag: after[i].path, payload: {} } });
    });
    return out;
  }

  // The stops under the player, to jump to. Stitched, each session begins with its date,
  // by the recording's clock like the times under it.
  function listPages(pages, start, player, sessions) {
    var list = $("[data-pages]");
    if (pages.length < 2) return;
    pages.forEach(function (p) {
      if (sessions && p.opens) {
        var s = sessions[p.session];
        list.appendChild(el("li", { class: "pages-session",
          text: "Session " + (p.session + 1) + " · " + utc(p.real).slice(0, 16) + " UTC" + (s.src ? " · from " + s.src : "") }));
      }
      var button = el("button", { type: "button" },
        el("span", { class: "mono", text: sessions ? utc(p.real).slice(11) : clock(p.at - start) }), " ", p.path);
      button.addEventListener("click", function () { player.goto(p.at - start, true); });
      list.appendChild(el("li", null, button));
    });
    list.hidden = false;
  }

  function play(events, starts, shifts, sessions) {
    if (events.length < 2) throw new Error("Too short to play.");
    status("");
    var Player = window.rrwebPlayer.default || window.rrwebPlayer.Player || window.rrwebPlayer;
    var target = $("[data-player]");
    var width = Math.min(target.clientWidth || 960, 1200);
    var pages = pagesOf(events, starts, shifts);
    var tags = Object.create(null);
    var color = getComputedStyle(root).getPropertyValue("--people").trim() || "#2a78d6";
    var player = new Player({
      target: target,
      props: { events: withMarkers(events, pages, tags, color), width: width, height: Math.round(width * 9 / 16),
               autoPlay: false, skipInactive: true, tags: tags }
    });
    var start = events[0].timestamp, end = events[events.length - 1].timestamp;
    listPages(pages, start, player, sessions);
    return { pages: pages.length, seconds: Math.max(1, Math.round((end - start) / 1000)) };
  }

  /* ---------------------------------------------------------------- start */

  $("[data-back]").href = MOUNT + "/admin/?site=" + encodeURIComponent(site) + "#visitors";
  $("[data-title]").textContent = visitor ? "Visitor" : "Session";
  $("[data-session]").textContent = (visitor || id) || "—";
  document.title = (visitor ? "Visitor" : "Replay") + " · carterlitics";

  if (!ID.test(visitor || id) || (vid && !ID.test(vid))) {
    status("No such recording.", true);
    return;
  }

  var when = $("[data-when]");

  if (visitor) {
    api("/visitor", { id: visitor }).then(function (v) {
      if (!v) throw new Error("No sessions kept for this visitor. Sessions are kept for a while, and only with consent.");
      show("[data-stats]", visitorTiles(v));
      show("[data-viewed]", viewedTable(v));
      var recorded = v.sessions.filter(function (s) { return s.rec; }).slice(-MAX_SESSIONS);
      var playing = {};
      recorded.forEach(function (s) { playing[s.id] = true; });
      show("[data-sessions]", sessionsTable(v, playing));
      if (!recorded.length) {
        status("None of this visitor's sessions was recorded.");
        return null;
      }
      status("Loading " + recorded.length + (recorded.length === 1 ? " recording…" : " recordings…"));
      return Promise.all(recorded.map(function (s) { return api("/recording", { id: s.id }); })).then(function (all) {
        var parts = [];
        all.forEach(function (events, k) {
          if (events && events.length >= 2) parts.push({ session: recorded[k], events: events });
        });
        if (!parts.length) throw new Error("Its recordings are past their days.");
        var stitched = stitch(parts);
        var shown = play(stitched.events, stitched.starts, stitched.shifts,
                         parts.map(function (p) { return p.session; }));
        when.textContent = site + " · " + parts.length + (parts.length === 1 ? " recorded session" : " recorded sessions") +
          (parts.length < v.totals.recorded ? " (the latest " + parts.length + ")" : "") +
          " · " + shown.seconds + " s played back to back · " + shown.pages + " pages";
      });
    }).catch(function (e) { status(e.message, true); });
    return;
  }

  api("/recording", { id: id }).then(function (events) {
    if (!events) throw new Error("Not found. Recordings are only kept for a while, and only with consent.");
    var shown = play(events, [0], [0], null);
    when.textContent = site + " · " + utc(events[0].timestamp) + " UTC · " + shown.seconds + " s · " +
      (shown.pages > 1 ? shown.pages + " pages · " : "") + events.length + " events";
  }).catch(function (e) { status(e.message, true); });

  if (vid) {
    api("/visitor", { id: vid }).then(function (v) {
      var s = v && v.sessions.filter(function (x) { return x.id === id; })[0];
      if (v) show("[data-stats]", s ? sessionTiles(s, v) : visitorTiles(v));
    }).catch(function () { /* the recording stands on its own */ });
  }
})();
