/* carterlitics console. The page is a static shell; everything comes from /_c/api/*,
   which answers only to an administrator's session (a 401 sends you to sign in).

   Visitor-supplied strings (paths, referrers, user agents, event names, error
   messages) only ever reach the page through textContent. */
(function () {
  "use strict";

  var root = document.querySelector("[data-app]");
  if (!root) return;
  var MOUNT = root.dataset.mount || "/_c";
  var API = MOUNT + "/api";
  var RANGES = [1, 7, 30, 90];
  var TABS = ["overview", "traffic", "bots", "visitors", "events", "errors"];
  var params = new URLSearchParams(location.search);
  var state = {
    site: params.get("site") || "",
    days: RANGES.indexOf(+params.get("days")) >= 0 ? +params.get("days") : 7,
    tab: TABS.indexOf(location.hash.slice(1)) >= 0 ? location.hash.slice(1) : "overview",
    me: null,
    report: null,
    cache: {}
  };
  var nf = new Intl.NumberFormat("en-US");
  var compact = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });
  var pct = new Intl.NumberFormat("en-US", { style: "percent", maximumFractionDigits: 1 });
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  var view = root.querySelector("[data-view]");

  /* ---------------------------------------------------------------- dom */

  function el(tag, attrs) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      var v = attrs[k];
      if (v === null || v === undefined || v === false) return;
      if (k === "text") node.textContent = v;
      else if (k === "class") node.className = v;
      else if (k === "style") node.setAttribute("style", v);
      else if (k.indexOf("on") === 0) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : v);
    });
    for (var i = 2; i < arguments.length; i++) add(node, arguments[i]);
    return node;
  }

  function add(node, child) {
    if (child === null || child === undefined || child === false) return;
    if (Array.isArray(child)) child.forEach(function (c) { add(node, c); });
    else node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }

  function status(text, bad) {
    var s = root.querySelector("[data-status]");
    s.textContent = text || "";
    s.hidden = !text;
    s.classList.toggle("status--bad", Boolean(bad));
  }

  /* ---------------------------------------------------------------- formatting */

  function num(n) { return nf.format(n || 0); }
  function big(n) { return (n || 0) >= 10000 ? compact.format(n) : nf.format(n || 0); }
  function stamp(iso) { return (iso || "").slice(0, 19).replace("T", " "); }
  function dayLabel(day) {
    var p = (day || "").split("-");
    return MONTHS[+p[1] - 1] + " " + +p[2];
  }
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
  function clip(text, wide) {
    return el("span", { class: "clip" + (wide ? " clip--wide" : ""), text: text || "", title: text || "" });
  }
  function short(id, n) { return el("span", { class: "mono", text: (id || "").slice(0, n || 8), title: id || "" }); }
  function where(row) {
    var parts = [row.country, row.asn ? "AS" + row.asn : "", row.network].filter(Boolean);
    return clip(parts.join(" · "));
  }
  function replayLink(sid, label) {
    return el("a", { href: MOUNT + "/admin/replay?site=" + encodeURIComponent(state.site) + "&id=" + encodeURIComponent(sid),
      text: label || "Replay" });
  }

  /* ---------------------------------------------------------------- data */

  function get(path, query) {
    var q = new URLSearchParams(Object.assign({ site: state.site }, query || {}));
    return fetch(API + path + "?" + q, { credentials: "same-origin", cache: "no-store" }).then(function (res) {
      if (res.status === 401) {
        location.replace(MOUNT + "/auth/login?next=" + encodeURIComponent(location.pathname + location.search + location.hash));
        return new Promise(function () {});
      }
      if (!res.ok) throw new Error(res.status + " " + res.statusText);
      return res.json();
    });
  }

  function cached(key, load) {
    var k = state.site + "|" + state.days + "|" + key;
    if (!state.cache[k]) {
      state.cache[k] = load().catch(function (e) { delete state.cache[k]; throw e; });
    }
    return state.cache[k];
  }

  function logOf(kind) {
    return cached("log:" + kind, function () {
      return get("/log", { kind: kind, days: Math.min(state.days, 90), limit: 300 }).then(function (d) { return d.items; });
    });
  }

  /* ---------------------------------------------------------------- figures */

  function tiles(list) {
    return el("div", { class: "tiles" }, list.map(function (t) {
      return el("div", { class: "tile" },
        el("p", { class: "tile-label", text: t[0] }),
        el("p", { class: "tile-value", text: t[1], title: t[3] || null }),
        t[2] ? el("p", { class: "tile-note", text: t[2] }) : null);
    }));
  }

  /* The gridline above the tallest column: an even step of a power of ten. */
  function niceMax(n) {
    if (n <= 4) return 4;
    var mag = Math.pow(10, Math.floor(Math.log10(n)));
    var steps = [1, 2, 4, 6, 8, 10];
    for (var i = 0; i < steps.length; i++) if (steps[i] * mag >= n) return steps[i] * mag;
    return 10 * mag;
  }

  /* Columns per day. `keys` stack bottom-up; `extra` rows only show in the tooltip. */
  function columns(rows, keys, extra) {
    var total = function (r) { return keys.reduce(function (s, k) { return s + (r[k.key] || 0); }, 0); };
    var top = niceMax(Math.max.apply(null, rows.map(total).concat([0])));
    var tip = el("div", { class: "tip", hidden: true });
    var chart = el("div", { class: "chart" });

    function show(bar, r) {
      chart.querySelectorAll(".col.is-on").forEach(function (n) { n.classList.remove("is-on"); });
      bar.classList.add("is-on");
      tip.replaceChildren(el("div", { class: "tip-day", text: dayLabel(r.day) + ", " + r.day.slice(0, 4) }),
        keys.slice().reverse().map(function (k) {
          return el("div", { class: "tip-row" }, el("i", { class: "tip-key", style: "--key:" + k.color }),
            el("strong", { text: num(r[k.key]) }), el("span", { text: k.label }));
        }),
        (extra || []).map(function (k) {
          return el("div", { class: "tip-row" }, el("i", { class: "tip-key", style: "--key:transparent" }),
            el("strong", { text: k.format ? k.format(r) : num(r[k.key]) }), el("span", { text: k.label }));
        }));
      tip.hidden = false;
      var c = chart.getBoundingClientRect(), b = bar.getBoundingClientRect();
      var half = tip.offsetWidth / 2;
      tip.style.left = Math.max(half, Math.min(c.width - half, b.left + b.width / 2 - c.left)) + "px";
    }
    function hide() {
      tip.hidden = true;
      chart.querySelectorAll(".col.is-on").forEach(function (n) { n.classList.remove("is-on"); });
    }

    var cols = el("ol", { class: "cols", "aria-label": keys.map(function (k) { return k.label; }).join(" and ") + " per day" },
      rows.map(function (r) {
        var segments = keys.slice().reverse().filter(function (k) { return r[k.key] > 0; }).map(function (k) {
          return el("span", { class: "seg-fill seg-fill--" + k.cls, style: "height:" + (r[k.key] / top * 100) + "%" });
        });
        var label = dayLabel(r.day) + ": " + keys.map(function (k) { return num(r[k.key]) + " " + k.label.toLowerCase(); }).join(", ");
        var bar = el("button", { type: "button", class: "col", "aria-label": label }, segments);
        bar.addEventListener("pointerenter", function () { show(bar, r); });
        bar.addEventListener("focus", function () { show(bar, r); });
        bar.addEventListener("blur", hide);
        return el("li", null, bar);
      }));
    cols.addEventListener("pointerleave", hide);

    var mid = rows[Math.floor((rows.length - 1) / 2)] || rows[0];
    add(chart, [
      keys.length > 1 ? el("p", { class: "legend" }, keys.map(function (k) {
        return el("span", { style: "--key:" + k.color, text: k.label });
      })) : null,
      el("div", { class: "plot" },
        el("span", { class: "gridline", style: "--y:100%" }, el("span", { text: big(top) })),
        el("span", { class: "gridline", style: "--y:50%" }, el("span", { text: big(top / 2) })),
        el("span", { class: "gridline gridline--base", style: "--y:0%" }, el("span", { text: "0" })),
        cols),
      el("div", { class: "axis", "aria-hidden": "true" },
        el("span", { text: rows.length ? dayLabel(rows[0].day) : "" }),
        rows.length > 2 ? el("span", { text: dayLabel(mid.day) }) : null,
        el("span", { text: rows.length > 1 ? "Today" : "" })),
      tip,
      el("details", { class: "table-view" }, el("summary", { text: "Table" }),
        el("div", { class: "table-wrap" }, el("table", { class: "log" },
          el("thead", null, el("tr", null, el("th", { text: "Day (UTC)" }),
            keys.concat(extra || []).map(function (k) { return el("th", { class: "num", text: k.label }); }))),
          el("tbody", null, rows.slice().reverse().map(function (r) {
            return el("tr", null, el("td", { class: "when", text: r.day }), keys.concat(extra || []).map(function (k) {
              return el("td", { class: "num", text: k.format ? k.format(r) : num(r[k.key]) });
            }));
          })))))]);
    return chart;
  }

  /* A ranked list: name with a share bar, then numbers. */
  function ranked(rows, opts) {
    opts = opts || {};
    if (!rows || !rows.length) return el("p", { class: "empty", text: opts.empty || "Nothing yet." });
    var most = rows[0].value || 1;
    var label = opts.label || function (r) { return r.name; };
    return el("table", { class: "ranked" },
      opts.head ? el("thead", null, el("tr", null, el("th", { text: opts.head[0] }),
        opts.head.slice(1).map(function (h) { return el("td", { class: "num", text: h }); }))) : null,
      el("tbody", null, rows.slice(0, opts.limit || 12).map(function (r) {
        return el("tr", null,
          el("th", { scope: "row" }, el("span", { class: "name", style: "--w:" + (r.value / most * 100) + "%",
            text: label(r), title: r.name })),
          el("td", { class: "num num--lead", text: num(r.value) }),
          (opts.more || []).map(function (f) { return el("td", { class: "num", text: f(r) }); }));
      })));
  }

  function card(title, sub, body) {
    return el("section", { class: "card" }, el("h2", { text: title }),
      sub ? el("p", { class: "sub", text: sub }) : null, body);
  }

  function table(head, rows, empty) {
    if (!rows.length) return el("p", { class: "empty", text: empty || "Nothing yet." });
    return el("div", { class: "table-wrap" }, el("table", { class: "log" },
      el("thead", null, el("tr", null, head.map(function (h) {
        return el("th", { class: h.charAt(0) === "#" ? "num" : null, text: h.replace(/^#/, "") });
      }))),
      el("tbody", null, rows.map(function (cells) {
        return el("tr", null, cells.map(function (c) {
          return c && c.td ? c.td : el("td", null, c);
        }));
      }))));
  }

  /* A log table with a filter box that matches any cell's text. */
  function searchable(head, rows, empty) {
    var box = el("div");
    var input = el("input", { type: "search", placeholder: "Filter " + rows.length + " rows", "aria-label": "Filter rows" });
    function draw() {
      var q = input.value.trim().toLowerCase();
      var shown = q ? rows.filter(function (r) { return r.text.indexOf(q) >= 0; }) : rows;
      box.replaceChildren(table(head, shown.map(function (r) { return r.cells; }), q ? "No rows match." : empty));
    }
    input.addEventListener("input", draw);
    draw();
    return [el("div", { class: "toolbar" }, input), box];
  }

  function row(cells, item) {
    return { cells: cells, text: Object.keys(item).map(function (k) { return String(item[k]); }).join(" ").toLowerCase() };
  }

  function td(value, cls) { return { td: el("td", { class: cls }, value) }; }

  /* ---------------------------------------------------------------- views */

  function overview(r) {
    var t = r.totals;
    var avg = t.pv ? t.ms / t.pv : 0;
    var noLogs = !t.req;
    return [
      tiles([
        ["Page views", big(t.pv), "people running JavaScript", num(t.pv)],
        ["Visitors", big(t.uv), "unique per day, summed", num(t.uv)],
        ["Engaged time", dur(avg), "visible time per view"],
        ["Requests", big(t.req), noLogs ? "no access logs yet" : "every page request at the edge", num(t.req)],
        ["Bots", t.req ? pct.format(t.bot / t.req) : "—", t.req ? num(t.bot) + " bot requests" : "of requests"],
        ["Errors", big(t.err), t.err ? r.groups.length + " distinct" : "in the browser"]
      ]),
      el("div", { class: "grid" },
        card("Page views", "From c.js: people with JavaScript, after bots are taken out.",
          columns(r.series, [{ key: "pv", label: "Page views", cls: "people", color: "var(--people)" }],
            [{ key: "uv", label: "Visitors" }])),
        card("Requests at the edge", noLogs
          ? "From CloudFront's access logs. None have arrived yet: run infra/deploy.py logging for this site."
          : "From CloudFront's access logs: every page request, cache hits included.",
          columns(r.series, [{ key: "hum", label: "People", cls: "people", color: "var(--people)" },
            { key: "bot", label: "Bots", cls: "bots", color: "var(--bots)" }]))),
      el("div", { class: "grid" },
        card("Pages", null, ranked(r.top.paths, { head: ["Page", "Views", "Time"], more: [function (x) { return dur(x.avg_ms); }] })),
        card("Referrers", null, ranked(r.top.referrers, { head: ["Source", "Views"] })),
        card("Countries", null, ranked(r.top.countries, { head: ["Country", "Views"] }))),
      el("div", { class: "grid" },
        card("Devices", "By window width.", ranked(r.top.devices)),
        card("Browsers", null, ranked(r.top.browsers)),
        card("Systems", null, ranked(r.top.systems)))
    ];
  }

  function trafficRows(items, bots) {
    return items.map(function (i) {
      var req = i.method + " " + i.path + (i.query ? "?" + i.query : "");
      var cells = [el("span", { class: "when", text: stamp(i.at) })];
      if (bots) cells.push(el("span", { class: "pill pill--bot", text: i.bot, title: i.cat }));
      cells.push(el("span", { class: "mono", text: i.ip }), where(i), clip(req, true),
        td(el("span", { class: i.status >= 400 ? "bad" : null, text: String(i.status) }), "num"),
        clip(i.ref), clip(i.ua, true), i.dc ? el("span", { class: "pill", text: "datacenter" }) : i.edge);
      return row(cells, i);
    });
  }

  function traffic(r) {
    var holder = el("div", null, el("p", { class: "empty", text: "Loading requests…" }));
    logOf("hum").then(function (items) {
      holder.replaceChildren.apply(holder, searchable(
        ["Time (UTC)", "IP", "Where", "Request", "#Status", "Referrer", "User agent", "Edge"],
        trafficRows(items, false), "No requests from people yet."));
    }, function (e) { holder.replaceChildren(el("p", { class: "empty bad", text: e.message })); });
    return [
      tiles([
        ["People", big(r.totals.hum), "page requests at the edge"],
        ["From datacenters", big(r.totals.dc), "browser user agent, hosting network"],
        ["Bots", big(r.totals.bot), "see the Bots tab"]
      ]),
      el("div", { class: "grid" },
        card("Pages requested by people", "Server side, so it includes visitors who block scripts.",
          ranked(r.top.server_paths, { head: ["Page", "Requests"] })),
        card("Where people are", "Country of each request's address.", ranked(r.top.server_countries)),
        card("Responses", "Every request, people and bots.", ranked(r.top.statuses, { head: ["Status", "Requests"] }))),
      card("Requests from people", "Newest first, up to 300.", holder)
    ];
  }

  function bots(r) {
    var holder = el("div", null, el("p", { class: "empty", text: "Loading requests…" }));
    logOf("bot").then(function (items) {
      holder.replaceChildren.apply(holder, searchable(
        ["Time (UTC)", "Bot", "IP", "Where", "Request", "#Status", "Referrer", "User agent", "Edge"],
        trafficRows(items, true), "No bot requests yet."));
    }, function (e) { holder.replaceChildren(el("p", { class: "empty bad", text: e.message })); });
    var t = r.totals;
    return [
      tiles([
        ["Bot requests", big(t.bot), t.req ? pct.format(t.bot / t.req) + " of all requests" : ""],
        ["Distinct bots", num(r.top.bots.length), "by user agent and behaviour"],
        ["Running JavaScript", big(t.jsbot), "page views c.js dropped as bots"],
        ["Probes", num((r.top.bots.filter(function (b) { return b.name === "probe"; })[0] || {}).value),
          "requests for things a scanner looks for"]
      ]),
      el("div", { class: "grid" },
        card("Bots", null, ranked(r.top.bots, { head: ["Bot", "Requests"], limit: 20 })),
        card("Kinds", null, ranked(r.top.categories)),
        card("Networks", "The organisation that owns the address.", ranked(r.top.bot_networks, { limit: 15 }))),
      el("div", { class: "grid" },
        card("Paths bots asked for", null, ranked(r.top.bot_paths, { limit: 20 })),
        card("Addresses", null, ranked(r.top.bot_ips, { limit: 20, label: function (x) { return x.name; } }))),
      card("Bot requests", "Newest first, up to 300.", holder)
    ];
  }

  function visitors() {
    var sessions = el("div", null, el("p", { class: "empty", text: "Loading sessions…" }));
    var views = el("div", null, el("p", { class: "empty", text: "Loading page views…" }));
    cached("sessions", function () {
      return get("/sessions", { days: Math.min(state.days, 30) }).then(function (d) { return d.items; });
    }).then(function (items) {
      sessions.replaceChildren.apply(sessions, searchable(
        ["Started (UTC)", "Length", "#Pages", "#Events", "#Errors", "Landed on", "From", "IP", "Where", "Browser", "Visitor", "User", ""],
        items.map(function (s) {
          return row([el("span", { class: "when", text: stamp(s.started) }), span(s.started, s.last),
            td(num(s.pages), "num"), td(num(s.events), "num"),
            td(el("span", { class: s.errors ? "bad" : null, text: num(s.errors) }), "num"),
            clip(s.path), clip(s.ref), el("span", { class: "mono", text: s.ip || "" }), where(s),
            clip([s.browser, s.os, s.device].filter(Boolean).join(" · ")), short(s.vid), clip(s.uid),
            s.rec ? replayLink(s.id) : el("span", { class: "empty", text: "no recording" })], s);
        }), "No consented sessions yet."));
    }, function (e) { sessions.replaceChildren(el("p", { class: "empty bad", text: e.message })); });
    logOf("pv").then(function (items) {
      views.replaceChildren.apply(views, searchable(
        ["Time (UTC)", "Page", "IP", "Where", "Visitor", "Device", "Browser", "User agent", ""],
        items.map(function (v) {
          return row([el("span", { class: "when", text: stamp(v.at) }), clip(v.path), el("span", { class: "mono", text: v.ip }),
            where(v), short(v.vid), short(v.fp), clip([v.browser, v.os, v.device].filter(Boolean).join(" · ")),
            clip(v.ua, true), v.sid ? replayLink(v.sid) : ""], v);
        }), "No consented page views yet."));
    }, function (e) { views.replaceChildren(el("p", { class: "empty bad", text: e.message })); });
    return [
      card("Sessions", "Visitors who accepted tracking: one browser tab, across page loads. Recordings are kept " +
        ((state.me && state.me.retention.recordings) || 30) + " days.", sessions),
      card("Consented page views", "With address, network, visitor cookie and device fingerprint.", views)
    ];
  }

  function events(r) {
    var holder = el("div", null, el("p", { class: "empty", text: "Loading events…" }));
    logOf("ev").then(function (items) {
      holder.replaceChildren.apply(holder, searchable(
        ["Time (UTC)", "Event", "Properties", "Page", "Source", "Where", "Visitor", ""],
        items.map(function (e) {
          var props = e.props && typeof e.props === "object" ? JSON.stringify(e.props) : String(e.props || "");
          return row([el("span", { class: "when", text: stamp(e.at) }), el("strong", { text: e.name }),
            el("span", { class: "props clip clip--wide", text: props === "{}" ? "" : props, title: props }),
            clip(e.path), e.source || "", where(e), e.vid ? short(e.vid) : clip(e.uid), e.sid ? replayLink(e.sid) : ""], e);
        }), "No events yet. Send one with cl(\"track\", \"name\", { … })."));
    }, function (e) { holder.replaceChildren(el("p", { class: "empty bad", text: e.message })); });
    return [
      el("div", { class: "grid" },
        card("Events", "cl(\"track\", name, props) in the page, or POST " + MOUNT + "/server/events from a backend.",
          ranked(r.top.events, { head: ["Event", "Count"], limit: 30, empty: "No events in this range." }))),
      card("Recent events", "Newest first, up to 300.", holder)
    ];
  }

  function errorDetail(gid, cell) {
    cell.replaceChildren(el("p", { class: "empty", text: "Loading…" }));
    get("/error", { id: gid }).then(function (d) {
      var g = d.group;
      cell.replaceChildren(
        el("dl", { class: "kv" },
          el("dt", { text: "Kind" }), el("dd", { text: [g.kind, g.name].filter(Boolean).join(" · ") }),
          el("dt", { text: "Source" }), el("dd", { class: "mono", text: g.src || "—" }),
          el("dt", { text: "Seen" }), el("dd", { text: num(g.n) + " times, " + stamp(g.first) + " to " + stamp(g.last) + " UTC" })),
        g.stack ? el("pre", { class: "stack", text: g.stack }) : null,
        el("h2", { style: "font-size:13px;margin:14px 0 6px", text: "Latest occurrences" }),
        table(["Time (UTC)", "Page", "Browser", "Where", "User agent", ""], d.occurrences.map(function (o) {
          return [el("span", { class: "when", text: stamp(o.at) }), clip(o.path),
            clip([o.browser, o.os].filter(Boolean).join(" · ")), where(o), clip(o.ua, true),
            o.sid ? replayLink(o.sid) : ""];
        }), "None kept."));
    }, function (e) { cell.replaceChildren(el("p", { class: "empty bad", text: e.message })); });
  }

  function errors(r) {
    if (!r.groups.length) {
      return card("Errors", "Uncaught errors, unhandled rejections, failed resources and CSP violations, grouped.",
        el("p", { class: "empty", text: "No errors in this range." }));
    }
    var body = el("tbody");
    r.groups.forEach(function (g) {
      var tr = el("tr", { "data-gid": g.id, tabindex: "0", "aria-expanded": "false" },
        el("td", { class: "num", text: num(g.count) }),
        el("td", { class: "msg" }, el("strong", { text: g.name ? g.name + ": " : "" }), clip(g.message, true)),
        el("td", null, el("span", { class: "pill", text: g.kind })),
        el("td", null, clip(g.src || g.last_path)),
        el("td", { class: "when", text: stamp(g.first) }),
        el("td", { class: "when", text: stamp(g.last) }));
      var detail = el("tr", { hidden: true }, el("td", { colspan: "6" }));
      function toggle() {
        var open = detail.hidden;
        detail.hidden = !open;
        tr.classList.toggle("is-open", open);
        tr.setAttribute("aria-expanded", String(open));
        if (open && !detail.dataset.loaded) {
          detail.dataset.loaded = "1";
          errorDetail(g.id, detail.firstChild);
        }
      }
      tr.addEventListener("click", toggle);
      tr.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } });
      body.append(tr, detail);
    });
    return [
      tiles([["Errors", big(r.totals.err), "in this range"], ["Distinct", num(r.groups.length), "grouped by message and frame"]]),
      card("Error groups", "Click one for its stack and latest occurrences. Same message shape and top frame = same group.",
        el("div", { class: "table-wrap" }, el("table", { class: "log errors" },
          el("thead", null, el("tr", null, ["#Count", "Error", "Kind", "Where", "First (UTC)", "Last (UTC)"].map(function (h) {
            return el("th", { class: h.charAt(0) === "#" ? "num" : null, text: h.replace(/^#/, "") });
          }))), body)))
    ];
  }

  var VIEWS = { overview: overview, traffic: traffic, bots: bots, visitors: visitors, events: events, errors: errors };

  /* ---------------------------------------------------------------- shell */

  function render() {
    root.querySelectorAll("[data-tab]").forEach(function (a) {
      a.setAttribute("aria-current", a.dataset.tab === state.tab ? "page" : "false");
    });
    if (!state.report) return;
    view.replaceChildren.apply(view, [].concat(VIEWS[state.tab](state.report)));
  }

  function remember() {
    params.set("site", state.site);
    params.set("days", String(state.days));
    history.replaceState(null, "", location.pathname + "?" + params + "#" + state.tab);
  }

  function load() {
    remember();
    status(state.report ? "" : "Loading…");
    view.classList.add("is-loading");
    cached("report", function () { return get("/report", { days: state.days }); }).then(function (r) {
      state.report = r;
      status(r.configured ? "" : "No table is attached to the collector (TABLE), so nothing is being stored.", !r.configured);
      view.classList.remove("is-loading");
      render();
    }, function (e) {
      view.classList.remove("is-loading");
      status("Couldn't load the report (" + e.message + "). Reload to retry.", true);
    });
  }

  function ranges() {
    var group = root.querySelector("[data-range]");
    group.replaceChildren.apply(group, RANGES.map(function (n) {
      return el("button", { type: "button", "aria-pressed": String(n === state.days), text: n === 1 ? "Today" : n + " days",
        onclick: function () {
          if (n === state.days) return;
          state.days = n;
          ranges();
          load();
        } });
    }));
  }

  window.addEventListener("hashchange", function () {
    var tab = location.hash.slice(1);
    if (TABS.indexOf(tab) >= 0 && tab !== state.tab) {
      state.tab = tab;
      render();
    }
  });

  get("/me").then(function (me) {
    state.me = me;
    var ids = me.sites.map(function (s) { return s.id; });
    if (ids.indexOf(state.site) < 0) state.site = ids.indexOf(me.console) >= 0 ? me.console : ids[0] || "";
    var who = root.querySelector("[data-who]");
    who.textContent = me.user.name || me.user.id;
    who.title = me.user.id;
    var select = root.querySelector("[data-site]");
    select.replaceChildren.apply(select, me.sites.map(function (s) {
      return el("option", { value: s.id, text: s.id, selected: s.id === state.site });
    }));
    select.addEventListener("change", function () {
      state.site = select.value;
      state.report = null;
      load();
    });
    ranges();
    load();
  }, function (e) { status("Couldn't reach the collector (" + e.message + ").", true); });
})();
