/* Runs c.js in a stub page under Node, for tests/test_collector.py's GoogleCalls tests.

     node tests/client_harness.js '{"before": "<page code>", "after": "<page code>", ...}'

   `before` runs ahead of c.js (Google's snippet, queued calls), `after` once it has
   loaded. Page code can push to `log`, call `advance(ms)` to move the page's clock on,
   and `fire(target, type, event)` to dispatch an event to what c.js listens to on
   "window", "document" or "navigation". Prints what c.js sent, as JSON: { items,
   envelopes, posts, beacons, gets, log, probe }. Spec keys: dataset (the script tag's
   data-*), cookies, session (sessionStorage to start with), dnt, answers (what GETs get
   back, in turn, the last one repeating: a JSON body, a status, or null for a network
   error), url and referrer (the page's address, and where it was reached from),
   navigation (the page has the Navigation API: window.navigation, at history entry 1),
   prerendering (the page loads prerendered: page code shows it with
   `document.prerendering = false; fire("document", "prerenderingchange", {})`),
   rrweb (a stand-in recorder loads when c.js asks for it, a tick later: "record",
   "snapshot" and ["custom", tag, payload] go to `log`), tabs (the site's other tabs, through
   BroadcastChannel: what the page posts goes to `log` as ["tab", message], another tab
   answers its first `tabs` claims, and page code calls `tell(message)` to deliver one),
   wait (ms to let the page's own timers run before the last flush), probe (an expression
   evaluated last). */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const spec = JSON.parse(process.argv[2] || "{}");
const posts = [];
const log = [];
const cookies = Object.assign({}, spec.cookies || {});
const stored = Object.assign({}, spec.session || {});
const gets = [];
const answers = [].concat(spec.answers === undefined ? [null] : spec.answers);
const address = new URL(spec.url || "https://a.example/start");

// The page's clock, which `advance` moves on.
let skew = 0;
class Clock extends Date {
  constructor(...args) {
    if (args.length) super(...args);
    else super(Date.now() + skew);
  }
  static now() { return Date.now() + skew; }
}
let uuids = 0;

const listeners = { window: {}, document: {}, navigation: {} };
const listen = (where) => (type, fn) => { (listeners[where][type] = listeners[where][type] || []).push(fn); };
const beacons = [];

const page = {
  log,
  console,
  fire(where, type, event) { (listeners[where][type] || []).forEach((fn) => fn(event)); },
  Date: Clock,
  advance(ms) { skew += ms; },
  URL,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  innerWidth: 1280,
  Blob: class { constructor(parts) { this.text = parts.join(""); } },
  location: { href: address.href, pathname: address.pathname, search: address.search, protocol: address.protocol,
              host: address.host, origin: address.origin },
  navigator: {
    userAgent: "harness", languages: ["en"], doNotTrack: spec.dnt ? "1" : null,
    sendBeacon(url, blob) {
      posts.push({ url, body: JSON.parse(blob.text) });
      beacons.push(posts.length - 1);
      return true;
    }
  },
  history: { pushState() {}, replaceState() {} },
  sessionStorage: {
    getItem: (k) => (k in stored ? stored[k] : null),
    setItem: (k, v) => { stored[k] = String(v); },
    removeItem: (k) => { delete stored[k]; }
  },
  // 11111111-2222-4333-8444-555555555555 first (the tests' SID), then the next ones.
  crypto: { randomUUID: () => "11111111-2222-4333-8444-" + (0x555555555555 + uuids++).toString(16) },
  fetch(url, init) {
    if (init.method !== "POST") {
      gets.push(url);
      const answer = answers.length > 1 ? answers.shift() : answers[0];
      if (answer === null) return Promise.reject(new TypeError("Failed to fetch"));
      if (typeof answer === "number") return Promise.resolve({ ok: false, status: answer });
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(JSON.parse(JSON.stringify(answer))) });
    }
    posts.push({ url, body: JSON.parse(init.body) });
    return Promise.resolve({});
  },
  addEventListener: listen("window")
};
page.window = page;

const channels = [];
let tabAnswers = spec.tabs || 0;
page.BroadcastChannel = class {
  constructor() { channels.push(this); }
  postMessage(m) {
    log.push(["tab", JSON.parse(JSON.stringify(m))]);
    if (m.t === "claim" && tabAnswers > 0) {
      tabAnswers--;
      setTimeout(() => this.deliver({ t: "mine", sid: m.sid, to: m.from, from: "other-tab" }), 5);
    }
  }
  deliver(m) { if (!this.closed && this.onmessage) this.onmessage({ data: m }); }
  close() { this.closed = true; }
};
page.tell = (m) => channels.forEach((c) => c.deliver(m));
if (spec.navigation) {
  page.navigation = { currentEntry: { index: 1 }, transition: null, addEventListener: listen("navigation") };
}
page.document = {
  currentScript: { src: "https://a.example/_c/c.js", dataset: spec.dataset || {} },
  referrer: spec.referrer || "",
  visibilityState: "visible",
  prerendering: Boolean(spec.prerendering),
  get cookie() { return Object.keys(cookies).map((k) => k + "=" + cookies[k]).join("; "); },
  set cookie(v) {
    const pair = v.split(";")[0];
    const i = pair.indexOf("=");
    cookies[pair.slice(0, i)] = pair.slice(i + 1);
  },
  querySelector: () => null,
  addEventListener: listen("document"),
  createElement: () => ({ getContext: () => null }),
  head: {
    appendChild(el) {
      if (!spec.rrweb || !el.src || !el.onload) return;
      const record = () => { log.push("record"); return () => {}; };
      record.takeFullSnapshot = () => { log.push("snapshot"); };
      record.addCustomEvent = (tag, payload) => { log.push(["custom", tag, payload]); };
      page.rrweb = { record };
      setTimeout(el.onload, 0);
    }
  }
};

vm.createContext(page);
const client = fs.readFileSync(path.join(__dirname, "..", "carterlitics", "static", "c.js"), "utf8");

(async () => {
  vm.runInContext(spec.before || "", page, { filename: "before.js" });
  vm.runInContext(client, page, { filename: "c.js" });
  vm.runInContext(spec.after || "", page, { filename: "after.js" });
  if (spec.wait) await new Promise((resolve) => setTimeout(resolve, spec.wait));
  await page.carterlitics.flush();
  await new Promise((resolve) => setTimeout(resolve, 20));   // callbacks run after their send
  const probe = spec.probe ? vm.runInContext(spec.probe, page) : null;
  process.stdout.write(JSON.stringify({
    items: posts.flatMap((p) => p.body.items || []),
    envelopes: posts.map((p) => Object.assign({}, p.body, { items: undefined })),
    posts: posts.map((p) => p.body),
    beacons,
    gets,
    log,
    probe
  }));
  process.exit(0);   // c.js's own fallback timers (2 s callback timeouts) needn't run out
})().catch((e) => {
  process.stderr.write(String(e && e.stack || e));
  process.exit(1);
});
