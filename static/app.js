/* Read-only dashboard frontend. Polls /snapshot; never sends anything back
 * except that GET. No websockets, no auth — this only runs behind an SSH
 * tunnel to 127.0.0.1 on the VPS. */

const POLL_MS = 1500;
const STALE_AFTER_MS = 6000;

// Single source of truth for colours lives in index.html's :root CSS
// variables (bull/bear sampled from Bitunix's own live chart; the rest set
// per theme) — read here so the chart canvas, the fill markers and the
// surrounding UI can never drift apart or theme independently.
const rootStyle = getComputedStyle(document.documentElement);
const token = (name) => rootStyle.getPropertyValue(name).trim();
const BULL_COLOR = token("--bull");
const BEAR_COLOR = token("--bear");

const chartEl = document.getElementById("chart");
const chartWrapEl = document.getElementById("chart-wrap");
const chart = LightweightCharts.createChart(chartEl, {
  layout: {
    background: { color: token("--chart-bg") },
    textColor: token("--chart-text"),
    attributionLogo: false, // drop the TradingView watermark
  },
  grid: { vertLines: { color: token("--chart-grid") }, horzLines: { color: token("--chart-grid") } },
  // rightOffset keeps the newest candle (and its marker, if any) clear of
  // the price-scale/last-price-label chrome docked on the right edge.
  timeScale: {
    timeVisible: true,
    secondsVisible: true,
    rightOffset: 8,
    borderColor: token("--chart-axis-border"),
  },
  rightPriceScale: { borderColor: token("--chart-axis-border") },
  // Matches Bitunix/TradingView: wheel + drag zoom the time axis, drag-pan
  // through history, drag directly on either scale to rescale it. These
  // are the library defaults already — declared explicitly so a future
  // lightweight-charts upgrade can't silently change this dashboard's
  // interactivity out from under it.
  handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: true },
  handleScale: {
    mouseWheel: true,
    pinch: true,
    axisPressedMouseMove: { time: true, price: true },
  },
  kineticScroll: { touch: true, mouse: false },
});
const candleSeries = chart.addSeries(LightweightCharts.CandlestickSeries, {
  upColor: BULL_COLOR,
  downColor: BEAR_COLOR,
  borderVisible: false,
  wickUpColor: BULL_COLOR,
  wickDownColor: BEAR_COLOR,
});
new ResizeObserver((entries) => {
  const { width, height } = entries[0].contentRect;
  chart.resize(width, height);
}).observe(chartWrapEl);

const fillMarkers = new FillMarkersPrimitive({ bull: BULL_COLOR, bear: BEAR_COLOR });
candleSeries.attachPrimitive(fillMarkers);

let seenFillTimes = new Set();
// Fills are bucketed to their candle's open time, one badge per (bucket,
// side). An MM bot routinely fills several times inside a single 1m candle;
// plotting each individually puts multiple badges on the exact same x
// position, where they pile on top of each other and on the candle until
// it's unreadable. Extra fills in a bucket are absorbed silently.
let fillBuckets = new Map(); // `${bucketTime}_${side}` -> {time, side}
let candleIntervalS = 60;

// --- Timeframe selection ---
// The backend is the only thing that talks to Bitunix (its kline endpoint
// sends no CORS header, so the browser couldn't call it directly even if
// we wanted to) and it validates against the same whitelist server-side —
// this list is just for building the buttons.
const SUPPORTED_INTERVALS = ["1m", "3m", "10m", "15m", "30m", "1h", "2h", "6h"];
let currentInterval = "1m";
let intervalsRendered = false;

function renderTimeframeBar() {
  if (intervalsRendered) return;
  intervalsRendered = true;
  const bar = document.getElementById("timeframe-bar");
  bar.innerHTML = SUPPORTED_INTERVALS.map(
    (iv) => `<button class="tf-btn${iv === currentInterval ? " active" : ""}" data-interval="${iv}">${iv}</button>`
  ).join("");
  bar.addEventListener("click", (e) => {
    const btn = e.target.closest(".tf-btn");
    if (!btn || btn.dataset.interval === currentInterval) return;
    currentInterval = btn.dataset.interval;
    bar.querySelectorAll(".tf-btn").forEach((b) => b.classList.toggle("active", b === btn));

    // A timeframe switch changes what a "bucket" means, so old badges no
    // longer correspond to real bar boundaries — clear both the dedup set
    // and the rendered badges and let the next fetch rebuild them under the
    // new interval. The backend always returns the full fills window, so
    // nothing is lost.
    seenFillTimes = new Set();
    fillBuckets = new Map();
    fillMarkers.setMarkers([]);
    candleSeries.setData([]);
    updateChartLabel();
    poll(); // don't wait for the next tick — the click should feel instant
  });
}

function updateChartLabel() {
  document.getElementById("chart-label").innerHTML =
    `<b>${currentSymbol || "—"}</b> &middot; ${currentInterval} &middot; Last price Bitunix`;
}
let currentSymbol = "";

// The adapter itself only re-fetches from Bitunix every ~20s and caches
// between polls, so calling setData on every ~1.5s poll re-sends identical
// data most of the time — cheap for 200 rows, and simpler than trying to
// diff/patch just the in-progress candle.
function renderCandles(klines) {
  if (!klines || klines.length === 0) return;
  candleSeries.setData(
    klines.map((k) => ({ time: k.time, open: k.open, high: k.high, low: k.low, close: k.close }))
  );
  // Markers anchor to their candle's high/low, so the primitive needs the
  // bars keyed by time.
  fillMarkers.setCandles(new Map(klines.map((k) => [k.time, { high: k.high, low: k.low }])));
}

function addFillMarkers(fills, intervalS) {
  candleIntervalS = intervalS || candleIntervalS;
  let added = false;
  for (const f of fills) {
    const key = `${f.ts}-${f.side}-${f.price}`;
    if (seenFillTimes.has(key)) continue;
    seenFillTimes.add(key);

    const bucketTime = Math.floor(f.ts / candleIntervalS) * candleIntervalS;
    const bucketKey = `${bucketTime}_${f.side}`;
    if (fillBuckets.has(bucketKey)) continue; // already has a badge
    fillBuckets.set(bucketKey, { time: bucketTime, side: f.side });
    added = true;
  }
  if (!added) return;

  fillMarkers.setMarkers(Array.from(fillBuckets.values()).sort((a, b) => a.time - b.time));
}

function fmt(n, digits = 4) {
  if (n === null || n === undefined) return "—";
  return Number(n).toFixed(digits);
}

function renderPosition(venue) {
  const body = document.getElementById("position-body");
  const rows = [];

  for (const p of venue.positions || []) {
    const sideCls = p.side === "LONG" ? "long" : "short";
    const pnlCls = (p.unrealised_pnl ?? 0) >= 0 ? "pnl-pos" : "pnl-neg";
    rows.push(`
      <div class="pos-group">
        <span class="pos-side-label ${sideCls}">${p.side}</span>
        <div class="row"><span class="label">qty</span><span class="val">${fmt(p.qty_base, 3)}</span></div>
        <div class="row"><span class="label">entry</span><span class="val">${fmt(p.entry_price, 2)}</span></div>
        <div class="row"><span class="label">uPnL</span><span class="val ${pnlCls}">${fmt(p.unrealised_pnl, 4)}</span></div>
      </div>
    `);
  }
  if (!venue.positions || venue.positions.length === 0) {
    rows.push(`<div class="empty-note">no open position</div>`);
  }

  if (venue.account) {
    const a = venue.account;
    const pnlCls = (a.unrealised_pnl ?? 0) >= 0 ? "pnl-pos" : "pnl-neg";
    rows.push(`
      <div class="account-block">
        <div class="row"><span class="label">available</span><span class="val">${fmt(a.available, 2)}</span></div>
        <div class="row"><span class="label">margin used</span><span class="val">${fmt(a.margin_used, 2)}</span></div>
        <div class="row"><span class="label">cross uPnL</span><span class="val ${pnlCls}">${fmt(a.unrealised_pnl, 2)}</span></div>
      </div>
    `);
  } else {
    rows.push(`<div class="account-block empty-note">account margin unavailable</div>`);
  }

  body.innerHTML = rows.join("");
}

function renderBook(ob) {
  const asksEl = document.getElementById("book-asks");
  const bidsEl = document.getElementById("book-bids");
  const spreadEl = document.getElementById("book-spread");
  if (!ob) {
    asksEl.innerHTML = "";
    bidsEl.innerHTML = "";
    spreadEl.textContent = "—";
    return;
  }
  const depth = 12;
  const asks = ob.asks.slice(0, depth).reverse();
  const bids = ob.bids.slice(0, depth);
  const maxSize = Math.max(1e-9, ...asks.map((l) => l.size), ...bids.map((l) => l.size));
  const bookRow = (l, side) => {
    const pct = Math.min(100, (l.size / maxSize) * 100);
    return `<div class="book-row ${side}"><div class="depth-bar" style="width:${pct}%"></div><span>${fmt(l.price, 2)}</span><span>${fmt(l.size, 3)}</span></div>`;
  };
  asksEl.innerHTML = asks.map((l) => bookRow(l, "ask")).join("");
  bidsEl.innerHTML = bids.map((l) => bookRow(l, "bid")).join("");
  const spreadBps = ob.mid > 0 ? ((ob.best_ask - ob.best_bid) / ob.mid) * 10000 : 0;
  spreadEl.textContent = `spread ${fmt(ob.best_ask - ob.best_bid, 4)} (${fmt(spreadBps, 1)} bps)`;
}

const bookPanel = document.getElementById("book-panel");
const bookToggle = document.getElementById("book-toggle");
let bookVisible = false;
let lastOrderbook = null;
bookToggle.addEventListener("click", () => {
  bookVisible = !bookVisible;
  bookPanel.classList.toggle("visible", bookVisible);
  bookToggle.textContent = bookVisible ? "hide" : "show";
  renderBook(bookVisible ? lastOrderbook : null); // don't wait for next poll tick
});

let lastGoodPollTs = 0;

async function poll() {
  try {
    const resp = await fetch(`/snapshot?interval=${encodeURIComponent(currentInterval)}`, { cache: "no-store" });
    if (!resp.ok) throw new Error(`http ${resp.status}`);
    const data = await resp.json();
    const venue = data.venues && data.venues.bitunix;
    if (venue) {
      currentSymbol = venue.symbol || "";
      renderTimeframeBar();
      updateChartLabel();
      renderCandles(venue.klines);
      addFillMarkers(venue.fills || [], venue.kline_interval_s);
      renderPosition(venue);
      lastOrderbook = venue.orderbook || null;
      renderBook(bookVisible ? lastOrderbook : null);
    }
    lastGoodPollTs = Date.now();
  } catch (err) {
    // No visible status indicator by design — a monitoring dashboard that
    // silently goes stale is a real failure mode, so it's still logged for
    // anyone with devtools open, just not surfaced as UI chrome.
    console.warn("poll failed:", err.message);
  }
}

setInterval(() => {
  if (Date.now() - lastGoodPollTs > STALE_AFTER_MS) {
    console.warn(`snapshot stale: no successful poll in over ${STALE_AFTER_MS}ms`);
  }
}, STALE_AFTER_MS);

poll();
setInterval(poll, POLL_MS);
