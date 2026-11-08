/* Runs c.js in a stub page under Node, for tests/test_collector.py's GoogleCalls tests.

     node tests/client_harness.js '{"before": "<page code>", "after": "<page code>", ...}'

   `before` runs ahead of c.js (Google's snippet, queued calls), `after` once it has
   loaded. Page code can push to `log`. Prints what c.js sent, as JSON:
   { items, envelopes, log, probe }. Spec keys: dataset (the script tag's data-*),
   cookies, dnt, probe (an expression evaluated last). */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const spec = JSON.parse(process.argv[2] || "{}");
const posts = [];
const log = [];
const cookies = Object.assign({}, spec.cookies || {});
const stored = {};

const page = {
  log,
  console,
  URL,
  setTimeout,
  clearTimeout,
  innerWidth: 1280,
  Blob: class { constructor(parts) { this.text = parts.join(""); } },
  location: { href: "https://a.example/start", pathname: "/start", protocol: "https:", host: "a.example" },
  navigator: {
    userAgent: "harness", languages: ["en"], doNotTrack: spec.dnt ? "1" : null,
    sendBeacon(url, blob) { posts.push({ url, body: JSON.parse(blob.text) }); return true; }
  },
  history: { pushState() {}, replaceState() {} },
  sessionStorage: {
    getItem: (k) => (k in stored ? stored[k] : null),
    setItem: (k, v) => { stored[k] = String(v); },
    removeItem: (k) => { delete stored[k]; }
  },
  crypto: { randomUUID: () => "11111111-2222-4333-8444-555555555555" },
  fetch(url, init) {
    posts.push({ url, body: JSON.parse(init.body) });
    return Promise.resolve({});
  },
  addEventListener() {}
};
page.window = page;
page.document = {
  currentScript: { src: "https://a.example/_c/c.js", dataset: spec.dataset || {} },
  referrer: "",
  visibilityState: "visible",
  get cookie() { return Object.keys(cookies).map((k) => k + "=" + cookies[k]).join("; "); },
  set cookie(v) {
    const pair = v.split(";")[0];
    const i = pair.indexOf("=");
    cookies[pair.slice(0, i)] = pair.slice(i + 1);
  },
  querySelector: () => null,
  addEventListener() {},
  createElement: () => ({ getContext: () => null }),
  head: { appendChild() {} }
};

vm.createContext(page);
const client = fs.readFileSync(path.join(__dirname, "..", "carterlitics", "static", "c.js"), "utf8");

(async () => {
  vm.runInContext(spec.before || "", page, { filename: "before.js" });
  vm.runInContext(client, page, { filename: "c.js" });
  vm.runInContext(spec.after || "", page, { filename: "after.js" });
  await page.carterlitics.flush();
  await new Promise((resolve) => setTimeout(resolve, 20));   // callbacks run after their send
  const probe = spec.probe ? vm.runInContext(spec.probe, page) : null;
  process.stdout.write(JSON.stringify({
    items: posts.flatMap((p) => p.body.items || []),
    envelopes: posts.map((p) => Object.assign({}, p.body, { items: undefined })),
    log,
    probe
  }));
  process.exit(0);   // c.js's own fallback timers (2 s callback timeouts) needn't run out
})().catch((e) => {
  process.stderr.write(String(e && e.stack || e));
  process.exit(1);
});
