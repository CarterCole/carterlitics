/* Runs c.js in a stub page under Node, for tests/test_collector.py's GoogleCalls tests.

     node tests/client_harness.js '{"before": "<page code>", "after": "<page code>", ...}'

   `before` runs ahead of c.js (Google's snippet, queued calls), `after` once it has
   loaded. Page code can push to `log`, call `advance(ms)` to move the page's clock on,
   and `fire(target, type, event)` to dispatch an event to what c.js listens to on
   "window", "document" or "navigation". Prints what c.js sent, as JSON: { items,
   envelopes, posts, beacons, gets, log, console, probe }. Spec keys: dataset (the script tag's
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
   evaluated last).

   Experiments: `experiments` is what the collector puts in c.js for the site (a list), and
   `local` the localStorage to start with, and `delay` how long (ms) a GET's answer takes. For page code, `node(tag, attrs, selectors)` is an
   element on the page: attrs are its attributes (`data-cl-x`, `hidden`, `text` for its
   text), selectors the CSS selectors it answers to. The output adds `session` and `local` (the
   storage as c.js left it), `styles` ([key, css] of each <style> c.js put in), `nodes` (each
   element's hidden, text and attributes, in order) and `classes` (<html>'s classes).

   Forms, for page code: `form(attrs)` is a stand-in <form> (`form(attrs, "DIV")` a box
   that isn't one, for [data-cl-form]), and `form.add(tag, attrs)` a field in it ("INPUT",
   "SELECT", "TEXTAREA"; attrs are its attributes, with `type`, `value`, `checked` and
   `private` for one inside [data-private]); `loose(tag, attrs)` is a field in no form.
   Then what a visitor does, as the browser tells the page: `focus(el)` (blurring the field
   before), `blur(el)`, `type(el, value, inputType)` (an input event, "insertText" unless
   said), `invalid(el)`, `submit(form)`, and `elsewhere(ms)`: to another tab for that long
   and back, the focused field losing focus and getting it back. */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const spec = JSON.parse(process.argv[2] || "{}");
const posts = [];
const log = [];
const cookies = Object.assign({}, spec.cookies || {});
const stored = Object.assign({}, spec.session || {});
const kept = Object.assign({}, spec.local || {});
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

// What c.js prints, as each console.log call's arguments (stdout is the JSON below).
const said = [];

const page = {
  log,
  console: { log: (...args) => said.push(args.map(String)), debug() {}, info() {}, warn() {}, error() {} },
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
              host: address.host, origin: address.origin, replace(url) { log.push(["replace", url]); } },
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
  localStorage: {
    getItem: (k) => (k in kept ? kept[k] : null),
    setItem: (k, v) => { kept[k] = String(v); },
    removeItem: (k) => { delete kept[k]; }
  },
  // 11111111-2222-4333-8444-555555555555 first (the tests' SID), then the next ones.
  crypto: { randomUUID: () => "11111111-2222-4333-8444-" + (0x555555555555 + uuids++).toString(16) },
  fetch(url, init) {
    if (init.method !== "POST") {
      gets.push(url);
      const answer = answers.length > 1 ? answers.shift() : answers[0];
      // A slow collector: the answer comes `delay` ms later.
      const later = spec.delay ? new Promise((resolve) => setTimeout(resolve, spec.delay)) : Promise.resolve();
      return later.then(() => {
        if (answer === null) throw new TypeError("Failed to fetch");
        if (typeof answer === "number") return { ok: false, status: answer };
        return { ok: true, status: 200, json: () => Promise.resolve(JSON.parse(JSON.stringify(answer))) };
      });
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
  querySelectorAll: (sel) => nodes.filter((n) => n.matches(sel)),
  addEventListener: listen("document"),
  createElement: (tag) => (tag === "style" ? element("STYLE", {}, null) : { getContext: () => null }),
  documentElement: {
    classes: new Set(spec.classes || []),
    get classList() {
      const c = this.classes;
      return { add: (k) => c.add(k), remove: (k) => c.delete(k), contains: (k) => c.has(k) };
    }
  },
  head: {
    appendChild(el) {
      if (el.tagName === "STYLE") {
        styles.push(el);
        el.parentNode = { removeChild: (x) => styles.splice(styles.indexOf(x), 1) };
        return;
      }
      if (!spec.rrweb || !el.src || !el.onload) return;
      const record = (options) => { log.push("record"); page.rrwebOptions = options; return () => { log.push("stop"); }; };
      record.takeFullSnapshot = () => { log.push("snapshot"); };
      record.addCustomEvent = (tag, payload) => { log.push(["custom", tag, payload]); };
      page.rrweb = { record };
      setTimeout(el.onload, 0);
    }
  }
};

// Stand-in forms and fields, and elements on the page: what c.js asks of them, no more.
const forms = [];
const nodes = [];
const styles = [];
page.document.forms = forms;
page.document.activeElement = null;
let windowFocus = true;
page.document.hasFocus = () => windowFocus;

function element(tag, attrs, box) {
  attrs = Object.assign({}, attrs);
  const flags = { private: Boolean(attrs.private) };
  const el = {
    nodeType: 1, tagName: tag, box: box || null, form: box && box.tagName === "FORM" ? box : null,
    type: attrs.type || (tag === "INPUT" ? "text" : tag === "SELECT" ? "select-one" : tag.toLowerCase()),
    value: attrs.value || "", checked: Boolean(attrs.checked), autofilled: false,
    hidden: Boolean(attrs.hidden), textContent: attrs.text || "", selectors: [],
    getAttribute: (k) => (k in attrs && !["value", "checked", "private", "hidden", "text"].includes(k) ? String(attrs[k]) : null),
    setAttribute: (k, v) => { attrs[k] = String(v); },
    attributes: () => attrs,
    closest(sel) {
      if (sel === "[data-private]") return flags.private ? {} : null;
      if (!el.box) return null;
      if (sel === "[data-cl-form]") return el.box.tagName !== "FORM" && el.box.getAttribute("data-cl-form") !== null ? el.box : null;
      return el.box;
    },
    matches: (sel) => (/autofill/.test(sel) ? el.autofilled
      : sel === "[data-cl-x]" ? "data-cl-x" in attrs : el.selectors.includes(sel)),
    querySelectorAll: () => []
  };
  return el;
}

page.node = (tag, attrs, selectors) => {
  const el = element(tag, attrs, null);
  el.selectors = selectors || [];
  nodes.push(el);
  return el;
};

page.form = (attrs, tag) => {
  const box = element(tag || "FORM", attrs);
  const fields = [];
  if (box.tagName === "FORM") {
    box.elements = fields;
    forms.push(box);
  } else {
    box.querySelectorAll = () => fields;
  }
  box.closest = (sel) => (sel === "[data-private]" ? null : box);
  box.add = (tag, a) => {
    const f = element(tag, a, box);
    fields.push(f);
    return f;
  };
  return box;
};
page.loose = (tag, attrs) => element(tag, attrs, null);
page.blur = (el) => {
  page.fire("document", "focusout", { target: el });
  if (page.document.activeElement === el) page.document.activeElement = null;
};
page.focus = (el) => {
  const was = page.document.activeElement;
  if (was && was !== el) page.blur(was);
  page.document.activeElement = el;
  page.fire("document", "focusin", { target: el });
};
page.type = (el, value, inputType) => {
  el.value = value;
  page.fire("document", "input", { target: el, inputType: inputType === undefined ? "insertText" : inputType });
};
page.invalid = (el) => page.fire("document", "invalid", { target: el });
page.submit = (box) => page.fire("document", "submit", { target: box });
page.elsewhere = (ms) => {
  const el = page.document.activeElement;
  windowFocus = false;
  if (el) page.fire("document", "focusout", { target: el });
  page.document.visibilityState = "hidden";
  page.fire("document", "visibilitychange", {});
  skew += ms;
  page.document.visibilityState = "visible";
  page.fire("document", "visibilitychange", {});
  windowFocus = true;
  if (el) page.fire("document", "focusin", { target: el });
};

vm.createContext(page);
let client = fs.readFileSync(path.join(__dirname, "..", "carterlitics", "static", "c.js"), "utf8");
if (spec.experiments) client = client.replace("/*__EXPERIMENTS__*/null", JSON.stringify(spec.experiments));

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
    console: said,
    probe,
    session: stored,
    local: kept,
    styles: styles.map((el) => [el.getAttribute("data-cl-style"), el.textContent]),
    nodes: nodes.map((el) => ({ hidden: el.hidden, text: el.textContent, attrs: el.attributes() })),
    classes: [...page.document.documentElement.classes]
  }));
  process.exit(0);   // c.js's own fallback timers (2 s callback timeouts) needn't run out
})().catch((e) => {
  process.stderr.write(String(e && e.stack || e));
  process.exit(1);
});
