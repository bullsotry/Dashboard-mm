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
const panel = doc.getElementById("stats-panel");
const grip = panel.querySelector(":scope > .panel-grip");
const pzOf = (el) => parseFloat(el.style.getPropertyValue("--pz"));

check("starts at 100%", pzOf(panel) === 1, `--pz=${pzOf(panel)}`);

const drag = (dy) => {
  const opts = (y) => ({ clientY: y, bubbles: true, cancelable: true });
  grip.dispatchEvent(new window.MouseEvent("pointerdown", opts(500)));
  grip.dispatchEvent(new window.MouseEvent("pointermove", opts(500 + dy)));
  grip.dispatchEvent(new window.MouseEvent("pointerup", opts(500 + dy)));
};

drag(110); // half of ZOOM_PX_PER_UNIT => +0.5
check("dragging DOWN enlarges", pzOf(panel) > 1.4 && pzOf(panel) < 1.6, `--pz=${pzOf(panel)}`);
check("the drag persisted", window.localStorage.getItem("dashboard.zoom.stats-panel") !== null,
      `stored=${window.localStorage.getItem("dashboard.zoom.stats-panel")}`);

drag(-330);
check("dragging UP shrinks below 1", pzOf(panel) < 1, `--pz=${pzOf(panel)}`);
check("clamped at the floor", pzOf(panel) >= 0.75, `--pz=${pzOf(panel)}`);

grip.dispatchEvent(new window.MouseEvent("dblclick", { bubbles: true }));
check("double-click resets to 100%", pzOf(panel) === 1, `--pz=${pzOf(panel)}`);

// Panels are independent.
const other = doc.getElementById("bots-panel");
check("zooming one panel leaves the others alone", pzOf(other) === 1, `--pz=${pzOf(other)}`);

// The two column handles must still be wired (they live above the zoom code).
const layout = doc.getElementById("layout");
const railHandle = doc.getElementById("rail-resize-handle");
railHandle.dispatchEvent(new window.MouseEvent("pointerdown", { clientX: 900, bubbles: true, cancelable: true }));
railHandle.dispatchEvent(new window.MouseEvent("pointermove", { clientX: 700, bubbles: true, cancelable: true }));
railHandle.dispatchEvent(new window.MouseEvent("pointerup", { clientX: 700, bubbles: true, cancelable: true }));
check("rail width handle still responds",
      layout.style.getPropertyValue("--rail-w") !== "",
      `--rail-w=${layout.style.getPropertyValue("--rail-w") || "(unset)"}`);

// Collapse must still work.
const toggle = doc.querySelector('button.panel-toggle[data-panel="bots-panel"]');
toggle.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
check("collapse still works", other.classList.contains("collapsed"));

console.log(failures === 0 ? "\nALL CHECKS PASSED" : `\n${failures} CHECK(S) FAILED`);
process.exit(failures === 0 ? 0 : 1);
