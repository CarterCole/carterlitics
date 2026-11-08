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
  var TABS = ["overview", "traffic", "bots", "visitors", "events", "funnels", "errors"];
  var params = new URLSearchParams(location.search);
  var state = {
    site: params.get("site") || "",
    days: RANGES.indexOf(+params.get("days")) >= 0 ? +params.get("days") : 7,
    tab: TABS.indexOf(location.hash.slice(1)) >= 0 ? location.hash.slice(1) : "overview",
    explore: { steps: params.get("steps") || "", by: params.get("by") === "visitor" ? "visitor" : "session" },
    me: null,
    report: null,
    cache: {},
    loaded: {}
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
  /* Kinds of connection (intel.py's net): how an address reaches the site. */
  var NET = { isp: "ISP or mobile", hosting: "Hosting or cloud", relay: "iCloud Private Relay", vpn: "VPN",
    tor: "Tor", crawler: "A crawler's own range" };
  var NET_PILL = { hosting: "datacenter", relay: "private relay", vpn: "vpn", tor: "tor" };
  function netLabel(r) { return NET[r.name] || r.name; }

  /* Country, place and network, with the reverse DNS name under it; `marks` adds flags. */
  function where(row, marks) {
    var place = [row.city, row.region].filter(Boolean).join(", ");
    var parts = [row.country, place, row.asn ? "AS" + row.asn : "", row.network].filter(Boolean);
    var title = [parts.join(" · "), row.host, row.svc].filter(Boolean).join("\n");
    return el("span", { class: "where" },
      el("span", { class: "clip", text: parts.join(" · "), title: title }),
      row.host ? el("span", { class: "clip host", text: row.host, title: row.host }) : null,
      marks ? flags(row) : null);
  }

  /* What's notable about a record's address: a verified or fake crawler, the kind of
     connection, a browser clock elsewhere, a recent attacker. */
  function flags(row) {
    var out = [];
    var net = row.net || (row.dc ? "hosting" : "");
    if (row.ver === "verified") out.push(el("span", { class: "pill pill--good", text: "verified",
      title: "The address is the crawler's own" }));
    if (row.ver === "spoofed") out.push(el("span", { class: "pill pill--bad", text: "fake",
      title: "Says it is a crawler; its address says otherwise" }));
    if (NET_PILL[net]) out.push(el("span", { class: "pill", text: NET_PILL[net], title: row.svc || NET[net] }));
    if (row.tzx) out.push(el("span", { class: "pill", text: "clock elsewhere",
      title: "The browser keeps " + (row.tz || "another zone") + "'s time, not the address's" }));
    if (row.abuse) out.push(el("span", { class: "pill pill--bad", text: "attacker", title: "Listed by " + row.abuse }));
    return out.length ? el("span", { class: "flags" }, out) : null;
  }

  /* An address you can click for everything known about it. */
  function ipCell(ip) {
    if (!ip) return "";
    return el("button", { type: "button", class: "ip", text: ip, title: "Look up " + ip,
      onclick: function (e) { e.stopPropagation(); inspect(ip); } });
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
      if (!res.ok) {
        return res.json().catch(function () { return {}; }).then(function (d) {
          throw new Error(d.error || res.status + " " + res.statusText);
        });
      }
      return res.json();
    });
  }

  function cached(key, load) {
    var k = state.site + "|" + state.days + "|" + key;
    if (!state.cache[k]) {
      state.cache[k] = load().then(function (v) { state.loaded[k] = v; return v; },
        function (e) { delete state.cache[k]; throw e; });
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

  /* ---------------------------------------------------------------- one address */

  var LOG_NAMES = { "log:hum": "request", "log:bot": "bot request", "log:pv": "page view", "log:ev": "event",
    sessions: "session" };
  var dialog = null;

  function inspect(ip) {
    if (!dialog) {
      dialog = el("dialog", { class: "ip-panel" });
      dialog.addEventListener("click", function (e) { if (e.target === dialog) dialog.close(); });
      document.body.append(dialog);
    }
    var body = el("div", null, el("p", { class: "empty", text: "Looking up " + ip + "…" }));
    dialog.setAttribute("aria-label", "Address " + ip);
    dialog.replaceChildren(
      el("div", { class: "ip-head" }, el("h2", { class: "mono", text: ip }),
        el("button", { type: "button", class: "btn btn--quiet", text: "Close", onclick: function () { dialog.close(); } })),
      body);
    if (!dialog.open) dialog.showModal();
    var seen = sightings(ip);
    function show(first) { body.replaceChildren(); add(body, [first, seen]); }
    get("/ip", { ip: ip }).then(function (d) { show(addressFacts(d)); },
      function (e) { show(el("p", { class: "empty bad", text: e.message })); });
  }

  function addressFacts(d) {
    var g = d.geo || {}, w = d.whois, rows = [];
    function kv(k, v) {
      if (v === null || v === undefined || v === "" || (Array.isArray(v) && !v.length)) return;
      rows.push(el("dt", { text: k }), el("dd", null, v));
    }
    kv("Place", [[g.city, g.region, g.country].filter(Boolean).join(", "), g.tz].filter(Boolean).join(" · "));
    kv("Network", [g.asn ? "AS" + g.asn : "", g.network].filter(Boolean).join(" · "));
    kv("Connection", [NET[d.net || "isp"], d.service && d.service !== NET[d.net] ? d.service : ""].filter(Boolean).join(" · "));
    kv("Reverse DNS", d.host === null ? (d.dns ? "no answer in time" : "not looked up (INTEL_DNS is off)")
      : d.host === "" ? "none"
      : [el("span", { class: "mono", text: d.host }), " ",
        d.confirmed === true ? el("span", { class: "pill pill--good", text: "resolves back" })
          : d.confirmed === false ? el("span", { class: "pill pill--bad", text: "doesn't resolve back" }) : null]);
    kv("Domain", d.domain);
    kv("On lists", d.ranges ? (d.tags.length ? el("span", { class: "flags" }, d.tags.map(function (t) {
      var bad = /^(drop|abuse):/.test(t);
      return el("span", { class: "pill" + (bad ? " pill--bad" : ""), text: t.replace(":", ": ") });
    })) : "none") : "no ranges table (infra/deploy.py intel)");
    if (d.ranges) kv("Lists from", /^\d{4}-/.test(d.ranges) ? stamp(d.ranges) + " UTC" : d.ranges);
    if (!d.global) kv("Scope", "Not a public address");
    if (w && w.error) kv("Registry", w.error);
    else if (w) {
      kv("Held by", [w.holder, w.address].filter(Boolean).join(" · "));
      kv("Block", [w.name, w.handle ? "(" + w.handle + ")" : "", w.type].filter(Boolean).join(" "));
      kv("Range", [w.start && w.end ? w.start + " – " + w.end : "", (w.cidrs || []).join(", ")].filter(Boolean).join(" · "));
      kv("Registered", [w.registered ? w.registered.slice(0, 10) : "", w.updated ? "changed " + w.updated.slice(0, 10) : ""].filter(Boolean).join(", "));
      kv("Abuse contact", w.abuse);
      kv("Notes", (w.remarks || []).join(" "));
      kv("Registry", [w.registry, w.country].filter(Boolean).join(" · "));
    }
    return el("dl", { class: "kv" }, rows);
  }

  /* The address in whatever logs this page has loaded, newest first. */
  function sightings(ip) {
    var prefix = state.site + "|" + state.days + "|", hits = [];
    Object.keys(state.loaded).forEach(function (k) {
      var kind = k.slice(prefix.length);
      if (k.indexOf(prefix) !== 0 || !LOG_NAMES[kind] || !Array.isArray(state.loaded[k])) return;
      state.loaded[k].forEach(function (i) { if (i.ip === ip) hits.push({ kind: LOG_NAMES[kind], item: i }); });
    });
    if (!hits.length) return el("p", { class: "sub", text: "Not among the newest requests and visits loaded here " +
      "(Traffic, Bots and Visitors each load their newest 300)." });
    hits.sort(function (a, b) { return (b.item.at || b.item.started || "") < (a.item.at || a.item.started || "") ? -1 : 1; });
    return [el("h3", { class: "ip-seen", text: num(hits.length) + " in the logs loaded here" }),
      table(["Time (UTC)", "What", "", "User agent"], hits.slice(0, 50).map(function (h) {
        var i = h.item;
        var what = i.method ? i.method + " " + i.path : i.name || i.path || "";
        return [el("span", { class: "when", text: stamp(i.at || i.started) }), clip(h.kind + ": " + what, true),
          i.bot ? el("span", { class: "pill pill--bot", text: i.bot }) : "", clip(i.ua, true)];
      }))];
  }

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
        card("Systems", null, ranked(r.top.systems)),
        card("Connections", "How page views reached the site. " + (t.tzx ? num(t.tzx) + " (" +
          pct.format(t.tzx / (t.pv || 1)) + ") came from a browser whose clock is in another time zone than its " +
          "address: travel, or a VPN." : "Browser and address time zones agree."),
          ranked(r.top.connections, { label: netLabel })))
    ];
  }

  function trafficRows(items, bots) {
    return items.map(function (i) {
      var req = i.method + " " + i.path + (i.query ? "?" + i.query : "");
      var cells = [el("span", { class: "when", text: stamp(i.at) })];
      if (bots) cells.push(el("span", { class: "pill pill--bot", text: i.bot, title: i.cat }));
      cells.push(ipCell(i.ip), where(i), clip(req, true),
        td(el("span", { class: i.status >= 400 ? "bad" : null, text: String(i.status) }), "num"),
        clip(i.ref), clip(i.ua, true), flags(i) || i.edge);
      return row(cells, i);
    });
  }

  function traffic(r) {
    var holder = el("div", null, el("p", { class: "empty", text: "Loading requests…" }));
    logOf("hum").then(function (items) {
      holder.replaceChildren.apply(holder, searchable(
        ["Time (UTC)", "IP", "Where", "Request", "#Status", "Referrer", "User agent", "Notes"],
        trafficRows(items, false), "No requests from people yet."));
    }, function (e) { holder.replaceChildren(el("p", { class: "empty bad", text: e.message })); });
    var conn = {};
    (r.top.server_connections || []).forEach(function (c) { conn[c.name] = c.value; });
    var hidden = (conn.relay || 0) + (conn.vpn || 0) + (conn.tor || 0);
    return [
      tiles([
        ["People", big(r.totals.hum), "page requests at the edge"],
        ["From datacenters", big(r.totals.dc), "browser user agent, hosting network"],
        ["Relays, VPNs, Tor", big(hidden), hidden ? num(conn.relay || 0) + " Private Relay · " + num(conn.vpn || 0) +
          " VPN · " + num(conn.tor || 0) + " Tor" : "address hidden behind one"],
        ["From attackers", big(r.totals.abuse), "addresses on blocklist.de or DShield, people and bots"],
        ["Bots", big(r.totals.bot), "see the Bots tab"]
      ]),
      el("div", { class: "grid" },
        card("Pages requested by people", "Server side, so it includes visitors who block scripts.",
          ranked(r.top.server_paths, { head: ["Page", "Requests"] })),
        card("Where people are", "Country of each request's address.", ranked(r.top.server_countries)),
        card("Responses", "Every request, people and bots.", ranked(r.top.statuses, { head: ["Status", "Requests"] }))),
      el("div", { class: "grid" },
        card("Connections", intelNote(r), ranked(r.top.server_connections, { label: netLabel })),
        card("Networks", "The organisation that owns the address.", ranked(r.top.server_networks, { limit: 15 })),
        card("Host domains", "From reverse DNS: an ISP, or a company with its own network.",
          ranked(r.top.server_domains, { limit: 15 }))),
      card("Requests from people", "Newest first, up to 300. Click an address for everything known about it.", holder)
    ];
  }

  function intelNote(r) {
    var i = r.intel || {};
    return (i.ranges ? "Published ranges from " + i.ranges.slice(0, 10) + "." : "No ranges table: run infra/deploy.py intel.") +
      (i.dns === false ? " Reverse DNS is off." : "");
  }

  function sum(rows) { return (rows || []).reduce(function (s, x) { return s + x.value; }, 0); }

  function bots(r) {
    var holder = el("div", null, el("p", { class: "empty", text: "Loading requests…" }));
    logOf("bot").then(function (items) {
      holder.replaceChildren.apply(holder, searchable(
        ["Time (UTC)", "Bot", "IP", "Where", "Request", "#Status", "Referrer", "User agent", "Notes"],
        trafficRows(items, true), "No bot requests yet."));
    }, function (e) { holder.replaceChildren(el("p", { class: "empty bad", text: e.message })); });
    var t = r.totals;
    return [
      tiles([
        ["Bot requests", big(t.bot), t.req ? pct.format(t.bot / t.req) + " of all requests" : ""],
        ["Distinct bots", num(r.top.bots.length), "by user agent and behaviour"],
        ["Running JavaScript", big(t.jsbot), "page views c.js dropped as bots"],
        ["Probes", num((r.top.bots.filter(function (b) { return b.name === "probe"; })[0] || {}).value),
          "requests for things a scanner looks for"],
        ["Verified crawlers", big(sum(r.top.verified)), "address checked: reverse DNS or published range"],
        ["Fake crawlers", big(sum(r.top.impostors)), "claimed a crawler's name from someone else's address"]
      ]),
      el("div", { class: "grid" },
        card("Bots", null, ranked(r.top.bots, { head: ["Bot", "Requests"], limit: 20 })),
        card("Kinds", null, ranked(r.top.categories)),
        card("Networks", "The organisation that owns the address.", ranked(r.top.bot_networks, { limit: 15 }))),
      el("div", { class: "grid" },
        card("Verified crawlers", intelNote(r), ranked(r.top.verified, { limit: 15,
          empty: "None checked yet: crawlers are verified by reverse DNS and their published ranges." })),
        card("Impostors", "Said they were these crawlers; the address said no.",
          ranked(r.top.impostors, { limit: 15, empty: "No fake crawlers in this range." })),
        card("Host domains", "From reverse DNS.", ranked(r.top.bot_domains, { limit: 15 }))),
      el("div", { class: "grid" },
        card("Paths bots asked for", null, ranked(r.top.bot_paths, { limit: 20 })),
        card("Addresses", "Click one for everything known about it.", addressList(r.top.bot_ips))),
      card("Bot requests", "Newest first, up to 300. Click an address for everything known about it.", holder)
    ];
  }

  /* A ranked list of addresses, each one clickable. */
  function addressList(rows) {
    var list = ranked(rows, { limit: 20 });
    list.querySelectorAll("tbody th .name").forEach(function (n) {
      var ip = n.textContent;
      n.replaceChildren(ipCell(ip));
    });
    return list;
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
            clip(s.path), clip(s.ref), ipCell(s.ip), where(s, true),
            clip([s.browser, s.os, s.device].filter(Boolean).join(" · ")), short(s.vid), clip(s.uid),
            s.rec ? replayLink(s.id) : el("span", { class: "empty", text: "no recording" })], s);
        }), "No consented sessions yet."));
    }, function (e) { sessions.replaceChildren(el("p", { class: "empty bad", text: e.message })); });
    logOf("pv").then(function (items) {
      views.replaceChildren.apply(views, searchable(
        ["Time (UTC)", "Page", "IP", "Where", "Visitor", "Device", "Browser", "User agent", ""],
        items.map(function (v) {
          return row([el("span", { class: "when", text: stamp(v.at) }), clip(v.path), ipCell(v.ip),
            where(v, true), short(v.vid), short(v.fp), clip([v.browser, v.os, v.device].filter(Boolean).join(" · ")),
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
            clip(e.path), e.source || "", where(e, true), e.vid ? short(e.vid) : clip(e.uid), e.sid ? replayLink(e.sid) : ""], e);
        }), "No events yet. Send one with cl(\"track\", \"name\", { … })."));
    }, function (e) { holder.replaceChildren(el("p", { class: "empty bad", text: e.message })); });
    return [
      el("div", { class: "grid" },
        card("Events", "cl(\"track\", name, props) in the page, or POST " + MOUNT + "/server/events from a backend.",
          ranked(r.top.events, { head: ["Event", "Count"], limit: 30, empty: "No events in this range." }))),
      card("Recent events", "Newest first, up to 300.", holder)
    ];
  }

  /* ---------------------------------------------------------------- funnels */

  var FUNNEL_EXAMPLE = [
    "[[site.funnel]]          # under the site's [[site]] table",
    "name = \"Sign-up\"",
    "by = \"session\"           # or \"visitor\": across sessions, by the visitor cookie",
    "steps = [\"/\", \"/pricing\", \"event:begin_checkout\", \"event:sign_up\"]"
  ].join("\n");

  function share(a, b) { return b ? pct.format(a / b) : "—"; }
  function seconds(s) { return s === null || s === undefined ? "—" : s < 1 ? "under 1s" : dur(s * 1000); }
  function failed(holder) {
    return function (e) { holder.replaceChildren(el("p", { class: "empty bad", text: e.message })); };
  }

  function funnelsOf(query) {
    return cached("funnels:" + JSON.stringify(query || {}), function () {
      return get("/funnels", Object.assign({ days: Math.min(state.days, 90) }, query || {}));
    });
  }

  /* One bar per step, its length the share of step 1. Hover or focus a row for the numbers. */
  function funnelFigure(f) {
    var first = f.steps[0].count;
    var unit = f.by === "visitor" ? "visitors" : "sessions";
    var tip = el("div", { class: "tip", hidden: true });
    var list = el("ol", { class: "funnel", "aria-label": f.name + ", " + unit + " per step" });
    var figure = el("div", { class: "funnel-fig" }, list, tip);

    function show(track, s, i) {
      tip.replaceChildren(
        el("div", { class: "tip-day", text: "Step " + (i + 1) + ": " + s.step }),
        el("div", { class: "tip-row" }, el("strong", { text: num(s.count) }), el("span", { text: unit })),
        el("div", { class: "tip-row" }, el("strong", { text: share(s.count, first) }), el("span", { text: "of step 1" })),
        i ? el("div", { class: "tip-row" }, el("strong", { text: share(s.count, f.steps[i - 1].count) }),
          el("span", { text: "of step " + i })) : null,
        i ? el("div", { class: "tip-row" }, el("strong", { text: seconds(s.median_s) }),
          el("span", { text: "median from step " + i })) : null);
      tip.hidden = false;
      var c = figure.getBoundingClientRect(), b = track.getBoundingClientRect();
      var fill = track.firstChild ? track.firstChild.getBoundingClientRect().right : b.left;
      var half = tip.offsetWidth / 2;
      tip.style.left = Math.max(half, Math.min(c.width - half, fill - c.left)) + "px";
      tip.style.top = (b.bottom - c.top + 6) + "px";
    }
    function hide() { tip.hidden = true; }

    f.steps.forEach(function (s, i) {
      var label = "Step " + (i + 1) + ", " + s.step + ": " + num(s.count) + " " + unit + ", " + share(s.count, first) + " of step 1";
      var track = el("button", { type: "button", class: "funnel-track", "aria-label": label },
        s.count ? el("span", { class: "funnel-fill", style: "width:" + (first ? s.count / first * 100 : 0) + "%" }) : null);
      track.addEventListener("pointerenter", function () { show(track, s, i); });
      track.addEventListener("pointerleave", hide);
      track.addEventListener("focus", function () { show(track, s, i); });
      track.addEventListener("blur", hide);
      add(list, el("li", { class: "funnel-step" },
        el("span", { class: "funnel-label" }, el("span", { class: "funnel-n", text: String(i + 1) }),
          el("span", { class: "clip clip--wide", text: s.step, title: s.step })),
        track,
        el("span", { class: "funnel-num" }, el("strong", { text: num(s.count) }), el("span", { text: share(s.count, first) }))));
      var next = f.steps[i + 1];
      if (!next || !s.count) return;
      var watch = s.dropped.filter(function (d) { return d.sid; }).slice(0, 5);
      add(list, el("li", { class: "funnel-drop" },
        el("span", { text: share(next.count, s.count) + " went on · " + num(s.count - next.count) + " stopped here" +
          (next.median_s !== null ? " · median " + seconds(next.median_s) + " to the next step" : "") }),
        watch.length && s.count > next.count ? el("span", null, " · watch ", watch.map(function (d, k) {
          return [k ? ", " : "", replayLink(d.sid, d.sid.slice(0, 6))];
        })) : null));
    });

    add(figure, el("details", { class: "table-view" }, el("summary", { text: "Table" }),
      el("div", { class: "table-wrap" }, el("table", { class: "log" },
        el("thead", null, el("tr", null, ["Step", "#" + unit.charAt(0).toUpperCase() + unit.slice(1), "#Of step 1",
          "#Of previous", "#Median from previous"].map(function (h) {
          return el("th", { class: h.charAt(0) === "#" ? "num" : null, text: h.replace(/^#/, "") });
        }))),
        el("tbody", null, f.steps.map(function (s, i) {
          return el("tr", null, el("td", { text: (i + 1) + ". " + s.step }), el("td", { class: "num", text: num(s.count) }),
            el("td", { class: "num", text: share(s.count, first) }),
            el("td", { class: "num", text: i ? share(s.count, f.steps[i - 1].count) : "—" }),
            el("td", { class: "num", text: i ? seconds(s.median_s) : "—" }));
        }))))));
    return figure;
  }

  function funnelSummary(f, d) {
    var total = f.by === "visitor" ? d.visitors : d.sessions;
    var unit = f.by === "visitor" ? "visitors" : "sessions";
    return "By " + (f.by === "visitor" ? "visitor" : "session") + ": " + num(f.entered) + " of " + num(total) +
      " consented " + unit + " reached step 1, and " + num(f.completed) + " (" + share(f.completed, f.entered) +
      ") the last.";
  }

  function funnels() {
    var mine = el("div", { class: "cards" }, el("p", { class: "empty", text: "Loading funnels…" }));
    funnelsOf(null).then(function (d) {
      if (!d.funnels.length) {
        mine.replaceChildren(card("Funnels", "None are set up for " + state.site + " yet. Try one below, then add it " +
          "to carterlitics.toml and deploy, and it shows here.", el("pre", { class: "stack", text: FUNNEL_EXAMPLE })));
        return;
      }
      mine.replaceChildren.apply(mine, d.funnels.map(function (f) {
        return card(f.name, funnelSummary(f, d), funnelFigure(f));
      }));
    }, failed(mine));

    var result = el("div");
    var area = el("textarea", { rows: "5", spellcheck: "false", "aria-label": "Funnel steps, one per line",
      placeholder: "/\n/pricing\nevent:sign_up" });
    area.value = state.explore.steps;
    var by = el("select", { "aria-label": "Count by" },
      el("option", { value: "session", text: "By session" }), el("option", { value: "visitor", text: "By visitor" }));
    by.value = state.explore.by;

    function explore() {
      var steps = area.value.split("\n").map(function (s) { return s.trim(); }).filter(Boolean);
      state.explore = { steps: steps.join("\n"), by: by.value };
      if (steps.length) params.set("steps", state.explore.steps); else params.delete("steps");
      params.set("by", by.value);
      remember();
      if (steps.length < 2) {
        result.replaceChildren(el("p", { class: "empty", text: "Give at least two steps, one per line." }));
        return;
      }
      result.replaceChildren(el("p", { class: "empty", text: "Counting…" }));
      funnelsOf({ steps: JSON.stringify(steps), by: by.value }).then(function (d) {
        var f = d.funnels[0];
        result.replaceChildren(el("p", { class: "sub", text: funnelSummary(f, d) }), funnelFigure(f));
      }, failed(result));
    }

    var form = el("form", { class: "explore", onsubmit: function (e) { e.preventDefault(); explore(); } },
      area,
      el("div", { class: "toolbar" }, el("label", { class: "field" }, by),
        el("button", { type: "submit", class: "btn", text: "Count" }),
        el("span", { class: "hint", text: "/path · /path/* · event:name · event:name?key=value" })));
    if (state.explore.steps) explore();

    return [
      mine,
      card("Try a funnel", "Steps in order, one per line. Counted over the consented sessions in this range; " +
        "anonymous visits carry nothing that ties one page to the next.", [form, result])
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
            clip([o.browser, o.os].filter(Boolean).join(" · ")), where(o, true), clip(o.ua, true),
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

  var VIEWS = { overview: overview, traffic: traffic, bots: bots, visitors: visitors, events: events, funnels: funnels,
    errors: errors };

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
