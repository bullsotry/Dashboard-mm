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
window.fetch = () => new Promise(() => {}); // never resolves: no polling noise

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

console.log(failures === 0 ? "\nALL CHECKS PASSED" : `\n${failures} CHECK(S) FAILED`);
process.exit(failures === 0 ? 0 : 1);
