/* carterlitics — one session recording, played back with rrweb-player. The events come
   from /_c/api/recording, which answers only to an administrator. The player rebuilds
   the recorded page in an iframe without scripts, and the page's Content-Security-Policy
   keeps a recorded page from loading anything but the sites' own files. */
(function () {
  "use strict";

  var root = document.querySelector("[data-replay]");
  if (!root) return;
  var MOUNT = root.dataset.mount || "/_c";
  var q = new URLSearchParams(location.search);
  var id = q.get("id") || "";
  var site = q.get("site") || "";
  var statusEl = root.querySelector("[data-status]");
  var target = root.querySelector("[data-player]");
  root.querySelector("[data-session]").textContent = id || "—";
  root.querySelector("[data-back]").href = MOUNT + "/admin/?site=" + encodeURIComponent(site) + "#visitors";

  function status(text, bad) {
    statusEl.textContent = text;
    statusEl.hidden = !text;
    statusEl.classList.toggle("status--bad", Boolean(bad));
  }

  if (!/^[0-9a-fA-F-]{8,64}$/.test(id)) {
    status("No such recording.", true);
    return;
  }

  fetch(MOUNT + "/api/recording?site=" + encodeURIComponent(site) + "&id=" + encodeURIComponent(id),
    { credentials: "same-origin", cache: "no-store" })
    .then(function (res) {
      if (res.status === 401) {
        location.replace(MOUNT + "/auth/login?next=" + encodeURIComponent(location.pathname + location.search));
        return null;
      }
      if (res.status === 404) throw new Error("Not found. Recordings are only kept for a while, and only with consent.");
      if (!res.ok) throw new Error("Couldn't load it (" + res.status + ").");
      return res.json();
    })
    .then(function (events) {
      if (!events) return;
      if (events.length < 2) throw new Error("Too short to play.");
      status("");
      var Player = window.rrwebPlayer.default || window.rrwebPlayer.Player || window.rrwebPlayer;
      var width = Math.min(target.clientWidth || 960, 1200);
      new Player({
        target: target,
        props: { events: events, width: width, height: Math.round(width * 9 / 16), autoPlay: false, skipInactive: true }
      });
      var start = events[0].timestamp, end = events[events.length - 1].timestamp;
      root.querySelector("[data-when]").textContent = site + " · " +
        new Date(start).toISOString().slice(0, 19).replace("T", " ") + " UTC · " +
        Math.max(1, Math.round((end - start) / 1000)) + " s · " + events.length + " events";
    })
    .catch(function (e) { status(e.message, true); });
})();
