// DOM smoke test for the dashboard's frontend controls.
//
// Not part of the pytest suite: it needs node + jsdom, which this
// otherwise-Python repo does not depend on. Run it by hand after touching
// static/app.js or the panel markup:
//
//     npm install jsdom        # once, anywhere on the path
//     node tests/frontend/dom_smoke.js
//
// Why it exists: `node -c` only checks syntax, and a page that throws
// halfway through app.js still renders and still polls — it just silently
// stops wiring up every control defined below the throw. That shipped
// twice. This loads the real index.html, evaluates the real scripts, and
// drives the controls with synthetic pointer events, so "the button does
// nothing" fails here instead of in front of the user.
//
// Seeding localStorage matters: the bug that motivated this only fired for
// users who had already dragged the order-book divider, because that is
// what makes the load-time code path run at all.
//
//     SEED_BOOK=420 node tests/frontend/dom_smoke.js
//
// The lightweight-charts stub below is deliberate — the real bundle needs a
// canvas jsdom does not provide, and none of these assertions are about
// chart rendering.

const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const ROOT = process.env.ROOT || path.join(__dirname, "..", "..", "static");
const html = fs.readFileSync(path.join(ROOT, "index.html"), "utf8");

const errors = [];
const dom = new JSDOM(html, {
  runScripts: "outside-only",
  pretendToBeVisual: true,
  url: "http://127.0.0.1:8091/",
});
const { window } = dom;

// jsdom has no PointerEvent and no pointer capture; the app only needs
// clientY plus the capture no-ops, so stub the minimum.
window.PointerEvent = window.MouseEvent;
window.Element.prototype.setPointerCapture = function () {};
window.Element.prototype.releasePointerCapture = function () {};
window.Element.prototype.hasPointerCapture = function () { return false; };
window.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
// Never resolves — a tunnel that has become a black hole, which is the
// scenario the poll timeout exists for. Counted, and honouring `signal`, so
// the timeout check below can watch the loop recover; before that timeout
// existed this stub would wedge the poll loop after exactly one request.
let fetchCalls = 0;
window.fetch = (url, opts = {}) => {
  fetchCalls++;
  return new Promise((_resolve, reject) => {
    if (opts.signal) {
      opts.signal.addEventListener("abort", () =>
        reject(Object.assign(new Error("aborted"), { name: "AbortError" })));
    }
  });
};
// Read by app.js in place of its 4500ms default, so this test takes
// milliseconds rather than seconds.
window.__POLL_TIMEOUT_MS__ = 40;

// Minimal lightweight-charts stand-in — the vendor bundle needs canvas.
const stubSeries = { setData() {}, update() {}, applyOptions() {}, setMarkers() {},
                     attachPrimitive() {}, detachPrimitive() {}, priceScale: () => ({ applyOptions() {} }) };
window.LightweightCharts = {
  createChart: () => ({
    addSeries: () => stubSeries, addCandlestickSeries: () => stubSeries,
    addHistogramSeries: () => stubSeries, addBaselineSeries: () => stubSeries,
    addLineSeries: () => stubSeries,
    timeScale: () => ({ fitContent() {}, applyOptions() {}, setVisibleLogicalRange() {},
                        getVisibleLogicalRange: () => null, subscribeVisibleLogicalRangeChange() {} }),
    priceScale: () => ({ applyOptions() {} }),
    applyOptions() {}, resize() {}, remove() {},
    subscribeCrosshairMove() {}, subscribeClick() {},
  }),
  CandlestickSeries: {}, HistogramSeries: {}, BaselineSeries: {}, LineSeries: {},
  ColorType: { Solid: "solid" }, LineStyle: { Dashed: 2, Solid: 0 }, CrosshairMode: { Normal: 0 },
};

// jsdom has no EventSource. Absent, app.js takes its poll-only fallback —
// which is what the default run of this file exercises. STREAM=1 installs a
// controllable one so the push path gets covered too; the two cannot share a
// run, because a healthy stream deliberately stands the poll down and the
// poll-timeout check below needs the poll running.
const STREAM = !!process.env.STREAM;
const sources = [];
if (STREAM) {
  window.EventSource = class {
    constructor(url) {
      this.url = url;
      this.listeners = {};
      this.closed = false;
      sources.push(this);
    }
    addEventListener(type, fn) {
      (this.listeners[type] = this.listeners[type] || []).push(fn);
    }
    close() { this.closed = true; }
    emit(type, data) {
      for (const fn of this.listeners[type] || []) fn({ data: JSON.stringify(data) });
    }
  };
}

if (process.env.SEED_BOOK) window.localStorage.setItem("dashboard.bookWidthPx", process.env.SEED_BOOK);
window.addEventListener("error", (e) => errors.push("window error: " + e.message));

// One eval, not three: each eval gets its own lexical scope, so a `class`
// declared in fill_markers.js would be invisible to app.js. The browser
// loads them as three <script> tags sharing one global scope; concatenating
// reproduces that.
const bundle = ["fill_markers.js", "price_tags.js", "app.js"]
  .map((f) => fs.readFileSync(path.join(ROOT, f), "utf8"))
  .join("\n;\n");
try {
  window.eval(bundle);
} catch (e) {
  errors.push(`threw during load: ${e.name}: ${e.message}`);
}

const doc = window.document;
let failures = 0;
const check = (name, cond, detail = "") => {
  console.log(`${cond ? "PASS" : "FAIL"}  ${name}${detail ? " — " + detail : ""}`);
  if (!cond) failures++;
};

check("app.js loaded with no exception", errors.length === 0, errors.join(" | "));

const grips = doc.querySelectorAll(".panel-grip");
check("a grip was appended to every zoomable panel", grips.length === 9,
      `found ${grips.length}, expected 9 (7 rail + book + curve)`);

check("main chart pane has no grip",
      !doc.getElementById("chart-pane").querySelector(":scope > .panel-grip"));

// Drive a real drag on the Performance panel.
// jsdom does no layout: every element measures 0x0, so getBoundingClientRect
// is stubbed to report whatever inline height the code set. That is exactly
// the quantity under test — the drag's arithmetic and what it writes back —
// and none of these assertions are about real layout.
const panel = doc.getElementById("stats-panel");
const body = doc.getElementById("stats-body");
const grip = panel.querySelector(":scope > .panel-grip");
const NATURAL = 120;
window.Element.prototype.getBoundingClientRect = function () {
  const h = this.style.height && this.style.height !== "auto"
    ? parseFloat(this.style.height)
    : NATURAL;
  return { height: h, width: 200, top: 0, left: 0, right: 200, bottom: h, x: 0, y: 0 };
};

const pzOf = (el) => parseFloat(el.style.getPropertyValue("--pz") || "1");
const heightOf = (el) => parseFloat(el.style.height || "0");

// `buttons` is what separates a drag from a hover, so every synthetic move
// has to carry it — 1 while the button is held, 0 once released.
const pev = (type, props) =>
  new window.MouseEvent(type, { bubbles: true, cancelable: true, ...props });

const drag = (dy, el = grip) => {
  el.dispatchEvent(pev("pointerdown", { clientY: 500, buttons: 1 }));
  el.dispatchEvent(pev("pointermove", { clientY: 500 + dy, buttons: 1 }));
  el.dispatchEvent(pev("pointerup", { clientY: 500 + dy, buttons: 0 }));
};

// The whole point of the redesign: the BOX moves with the mouse, 1:1, and
// the type does not move at all.
drag(100);
check("dragging down grows the box ~1:1 with the mouse",
      Math.abs(heightOf(body) - (NATURAL + 100)) < 2,
      `height=${heightOf(body)}px, expected ~${NATURAL + 100}px`);
check("...and the type is not scaled at all",
      panel.style.getPropertyValue("--pz") === "",
      `--pz='${panel.style.getPropertyValue("--pz")}' (must be unset: scaling type is what looked blurry)`);
check("the imposed height is a whole pixel",
      Number.isInteger(heightOf(body)), `height=${heightOf(body)}px`);
check("the drag persisted", window.localStorage.getItem("dashboard.panelSize.stats-panel") !== null,
      `stored=${window.localStorage.getItem("dashboard.panelSize.stats-panel")}`);

// --- Bug: the drag state must not survive the gesture ---------------------
// A hover over the grip after the drag has ended must do nothing. This is
// the regression that made the panel keep resizing under a released mouse.
const afterDrag = heightOf(body);
grip.dispatchEvent(pev("pointermove", { clientY: 900, buttons: 0 }));
grip.dispatchEvent(pev("pointermove", { clientY: 200, buttons: 0 }));
check("hovering the grip after a drag does not resize", heightOf(body) === afterDrag,
      `height went ${afterDrag}px → ${heightOf(body)}px with no button held`);

// Worst case: the pointerup never reaches the grip at all (released outside
// the window, capture silently dropped). The move handler's button check
// and the window-level up are the two backstops; verify each on its own.
grip.dispatchEvent(pev("pointerdown", { clientY: 500, buttons: 1 }));
grip.dispatchEvent(pev("pointermove", { clientY: 560, buttons: 1 }));
window.dispatchEvent(pev("pointerup", { clientY: 560, buttons: 0 }));
const afterLostUp = heightOf(body);
grip.dispatchEvent(pev("pointermove", { clientY: 900, buttons: 0 }));
check("a pointerup that lands on the window still ends the drag",
      heightOf(body) === afterLostUp,
      `height went ${afterLostUp}px → ${heightOf(body)}px`);
check("...and the grip drops its dragging class", !grip.classList.contains("dragging"));

grip.dispatchEvent(pev("pointerdown", { clientY: 500, buttons: 1 }));
grip.dispatchEvent(pev("pointermove", { clientY: 540, buttons: 1 }));
const midDrag = heightOf(body);
// No up anywhere — only a buttonless move, which is what a real hover after
// a lost release looks like. It must end the gesture, not continue it.
grip.dispatchEvent(pev("pointermove", { clientY: 800, buttons: 0 }));
grip.dispatchEvent(pev("pointermove", { clientY: 300, buttons: 0 }));
check("a buttonless move ends the drag instead of resizing",
      heightOf(body) === midDrag,
      `height went ${midDrag}px → ${heightOf(body)}px with no button and no pointerup`);

// Shrinking: the box must be free to get genuinely small.
const beforeShrink = heightOf(body);
drag(-200);
check("dragging up shrinks the box 1:1 too",
      Math.abs(heightOf(body) - (beforeShrink - 200)) < 2,
      `height=${beforeShrink}px → ${heightOf(body)}px, expected ~${beforeShrink - 200}px`);
drag(-1000);
check("the box cannot be dragged below the floor", heightOf(body) === 26,
      `height=${heightOf(body)}px`);

grip.dispatchEvent(new window.MouseEvent("dblclick", { bubbles: true }));
check("double-click clears the imposed height", body.style.height === "",
      `height='${body.style.height}'`);
check("double-click clears any legacy type scale", panel.style.getPropertyValue("--pz") === "");
check("double-click forgets the saved size",
      window.localStorage.getItem("dashboard.panelSize.stats-panel") === null);

// Panels are independent.
const other = doc.getElementById("bots-panel");
check("resizing one panel leaves the others alone",
      doc.getElementById("bots-body").style.height === "" && pzOf(other) === 1);

// Curve panel drives three charts from one grip.
const curveGrip = doc.getElementById("curve-panel").querySelector(":scope > .panel-grip");
drag(60, curveGrip);
check("one grip resizes all three Curve/Delta/Volume charts together",
      ["curve-chart", "delta-chart", "volume-curve-chart"]
        .every((id) => heightOf(doc.getElementById(id)) === NATURAL + 60),
      ["curve-chart", "delta-chart", "volume-curve-chart"]
        .map((id) => `${id}=${heightOf(doc.getElementById(id))}`).join(" "));

// The two column handles must still be wired (they live above the panel code).
const layout = doc.getElementById("layout");
const railHandle = doc.getElementById("rail-resize-handle");
railHandle.dispatchEvent(pev("pointerdown", { clientX: 900, buttons: 1 }));
railHandle.dispatchEvent(pev("pointermove", { clientX: 700, buttons: 1 }));
railHandle.dispatchEvent(pev("pointerup", { clientX: 700, buttons: 0 }));
check("rail width handle still responds",
      layout.style.getPropertyValue("--rail-w") !== "",
      `--rail-w=${layout.style.getPropertyValue("--rail-w") || "(unset)"}`);

// Same phantom-hover regression on the column handles.
const railAfter = layout.style.getPropertyValue("--rail-w");
railHandle.dispatchEvent(pev("pointermove", { clientX: 400, buttons: 0 }));
check("hovering the rail handle after a drag does not resize",
      layout.style.getPropertyValue("--rail-w") === railAfter,
      `--rail-w went ${railAfter} → ${layout.style.getPropertyValue("--rail-w")}`);

const bookHandle = doc.getElementById("book-resize-handle");
bookHandle.dispatchEvent(pev("pointerdown", { clientX: 500, buttons: 1 }));
bookHandle.dispatchEvent(pev("pointermove", { clientX: 450, buttons: 1 }));
window.dispatchEvent(pev("pointerup", { clientX: 450, buttons: 0 }));
const bookAfter = layout.style.getPropertyValue("--book-w");
bookHandle.dispatchEvent(pev("pointermove", { clientX: 100, buttons: 0 }));
check("hovering the book handle after a lost release does not resize",
      layout.style.getPropertyValue("--book-w") === bookAfter,
      `--book-w went ${bookAfter} → ${layout.style.getPropertyValue("--book-w")}`);

// Collapse must still work.
const toggle = doc.querySelector('button.panel-toggle[data-panel="bots-panel"]');
toggle.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
check("collapse still works", other.classList.contains("collapsed"));

// A hung request must not stop the poll loop. `pollInFlight` is released in
// a finally, but with no timeout the fetch above settles neither way, so the
// finally never runs and every later tick returns early — the page keeps
// ageing its badge while having silently stopped asking. Only the abort
// makes the loop resume, so a rising fetch count is the whole assertion.
const callsBefore = fetchCalls;
setTimeout(() => {
  if (!STREAM) {
    check("a hung poll times out and the loop resumes",
          fetchCalls > callsBefore,
          `fetch calls went ${callsBefore} → ${fetchCalls} while every request hangs`);
  } else {
    runStreamChecks();
  }

  console.log(failures === 0 ? "\nALL CHECKS PASSED" : `\n${failures} CHECK(S) FAILED`);
  process.exit(failures === 0 ? 0 : 1);
  // Long enough for the 40ms timeout above plus a 750ms poll tick to fire.
}, 2000);

// ── The push transport ─────────────────────────────────────────────────────
// Every check here is a way the stream could look connected while lying: a
// screen that never updates, a poll that keeps hammering anyway, a stale
// bot's frames landing after a switch, or — the dangerous one — a heartbeat
// resurrecting a bot that has stopped writing.
function runStreamChecks() {
  const snap = (ageS, equity) => {
    const now = Date.now() / 1000;
    return {
      server_ts: now,
      bot: "bitunix:SOLUSDT",
      bots: [{ key: "bitunix:SOLUSDT", label: "bitunix · SOLUSDT", exchange: "bitunix",
               symbol: "SOLUSDT", source_ts: now - ageS, positions: [], realised_net: 0 }],
      basis: { pairs: [], stale: [] },
      incidents: [],
      venue: {
        key: "bitunix:SOLUSDT", exchange: "bitunix", symbol: "SOLUSDT",
        label: "bitunix · SOLUSDT", source_ts: now - ageS,
        orderbook: { bids: [{ price: 1, size: 1 }], asks: [{ price: 2, size: 1 }], ts: now - ageS },
        positions: [], fills: [], quotes: [], stats: null, session: null, sessions: [],
        markouts: [], equity_curve: [], nav_curve: [],
        account: { equity: equity }, klines: null, klines_sig: "sig",
        kline_interval: "1m", kline_interval_s: 60, supported_intervals: ["1m"],
      },
    };
  };

  check("the stream was opened", sources.length > 0, `${sources.length} EventSource(s)`);
  const es = sources[sources.length - 1];
  check("...on /stream, carrying the selected interval",
        es && es.url.startsWith("/stream?") && es.url.includes("interval="), es && es.url);

  check("the interval poll stands down while the stream is healthy",
        fetchCalls === callsBefore,
        `fetch calls went ${callsBefore} → ${fetchCalls} with a live stream`);

  const errsBefore = errors.length;
  es.emit("snapshot", snap(1, 100));
  check("a pushed snapshot renders without throwing",
        errors.length === errsBefore, errors.slice(errsBefore).join(" | "));
  check("...and the badge reads live off the pushed frame",
        doc.getElementById("status-text").textContent === "live",
        doc.getElementById("status-text").textContent);

  // The one that matters most. A heartbeat proves the LINK is alive. If it
  // also refreshed bot age, a bot that stopped writing hours ago would sit
  // behind a green "live" badge — the exact confident lie this dashboard
  // exists to refuse.
  es.emit("snapshot", snap(3600, 100));
  check("a bot that stopped writing reads dead",
        doc.getElementById("status-text").textContent === "bot dead",
        doc.getElementById("status-text").textContent);
  es.emit("heartbeat", { server_ts: Date.now() / 1000 });
  check("a heartbeat does NOT resurrect a dead bot",
        doc.getElementById("status-text").textContent === "bot dead",
        `badge became "${doc.getElementById("status-text").textContent}" after a heartbeat`);

  // A switch closes the old connection; a frame still in flight from it is
  // the old bot's data and must not be applied on top of the new one.
  // The badge currently reads "bot dead". The superseded connection now
  // pushes a perfectly healthy frame: if it were applied, the badge would
  // flip to "live" — a dead bot made to look alive by a connection the user
  // already navigated away from. The badge is the discriminant, because
  // merely not throwing proves nothing.
  const before = sources.length;
  window.connectStream();
  const superseded = es;
  check("the old connection was closed on reconnect", superseded.closed === true);
  check("...and a new one was opened", sources.length > before,
        `${before} → ${sources.length} sources`);
  superseded.emit("snapshot", snap(1, 999));
  check("a frame from a superseded connection is ignored",
        doc.getElementById("status-text").textContent === "bot dead",
        `badge became "${doc.getElementById("status-text").textContent}" from a stale connection`);
}
