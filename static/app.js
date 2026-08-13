/* Read-only dashboard frontend. Polls /snapshot; never sends anything back
 * except that GET. No websockets, no auth — this only runs behind an SSH
 * tunnel to 127.0.0.1 on the VPS. */

const POLL_MS = 1500;
// Two independent failure modes, two independent clocks:
//   link  — we cannot reach our own server (tunnel dropped, uvicorn died).
//   bot   — the server answers fine, but the bot stopped writing its state.
// The second is the dangerous one: everything still renders, just frozen.
// 6s was tighter than a single round trip over an SSH tunnel to a loaded
// VPS, so an ordinary slow response — not a lost link — flipped the badge to
// "link down" and back every few seconds. Widening it makes the badge mean
// what it says; the numbers it guards are at most this stale, still far
// inside BOT_STALE_MS below, so nothing is being hidden by the change.
const LINK_STALE_MS = 12000;
const BOT_STALE_MS = 15000;
const BOT_DEAD_MS = 60000;
// Recomputed on a timer rather than per poll, so the age keeps counting up
// while polls are failing — a badge frozen at "2s" during an outage would
// reproduce the exact bug this is here to prevent.
const STATUS_TICK_MS = 500;

// Single source of truth for colours lives in index.html's :root CSS
// variables (bull/bear sampled from Bitunix's own live chart; the rest set
// per theme) — read here so the chart canvas, the fill markers and the
// surrounding UI can never drift apart or theme independently.
const rootStyle = getComputedStyle(document.documentElement);
const token = (name) => rootStyle.getPropertyValue(name).trim();
const BULL_COLOR = token("--bull");
const BEAR_COLOR = token("--bear");
// Dimmed variants for volume bars — support for the chart, not competing
// with it, same spirit as the order-book rail sitting under the price.
const hexToRgba = (hex, a) => {
  const n = parseInt(hex.replace("#", ""), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
};
const BULL_VOL_COLOR = hexToRgba(BULL_COLOR, 0.5);
const BEAR_VOL_COLOR = hexToRgba(BEAR_COLOR, 0.5);

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

// Volume overlay: own price scale pinned to the bottom of the same pane
// (margins below), never touching the candles' scale — same trick every
// OHLC chart uses so a full-height volume bar doesn't fight the price axis.
const volumeSeries = chart.addSeries(LightweightCharts.HistogramSeries, {
  priceFormat: { type: "volume" },
  priceScaleId: "volume",
});
chart.priceScale("volume").applyOptions({
  scaleMargins: { top: 0.82, bottom: 0 },
});

new ResizeObserver((entries) => {
  const { width, height } = entries[0].contentRect;
  chart.resize(width, height);
}).observe(chartWrapEl);

const fillMarkers = new FillMarkersPrimitive({ bull: BULL_COLOR, bear: BEAR_COLOR });
candleSeries.attachPrimitive(fillMarkers);

const priceTags = new PriceTagsPrimitive();
candleSeries.attachPrimitive(priceTags);

let seenFillTimes = new Set();
// Fills are bucketed to their candle's open time, one badge per (bucket,
// side). An MM bot routinely fills several times inside a single 1m candle;
// plotting each individually puts multiple badges on the exact same x
// position, where they pile on top of each other and on the candle until
// it's unreadable. Extra fills in a bucket are absorbed silently.
let fillBuckets = new Map(); // `${bucketTime}_${side}` -> {time, side}
let candleIntervalS = 60;

// --- Timeframe selection ---
// The buttons are whatever the *selected venue* reports it can serve, not a
// list hardcoded here: granularities differ per exchange (Bitunix has 3m and
// 10m, Coinbase has 5m and 1d), and a button for an interval the venue does
// not have would silently redraw the previous one. The backend, which is the
// only thing that talks to the exchanges (their kline endpoints send no CORS
// header), validates the same list server-side.
let supportedIntervals = [];
let currentInterval = "1m";
let tfListenerBound = false;

function renderTimeframeBar(venue) {
  const intervals = (venue && venue.supported_intervals) || [];
  // The server answers with the interval it actually served, which is how a
  // venue-unsupported request (just switched bots) gets corrected here.
  if (venue && venue.kline_interval) currentInterval = venue.kline_interval;

  const bar = document.getElementById("tf-buttons");
  const sig = intervals.join(",") + "|" + currentInterval;
  if (sig !== renderTimeframeBar._sig) {
    renderTimeframeBar._sig = sig;
    supportedIntervals = intervals;
    bar.innerHTML = intervals
      .map(
        (iv) =>
          `<button class="tf-btn${iv === currentInterval ? " active" : ""}" data-interval="${iv}">${iv}</button>`
      )
      .join("");
  }
  if (tfListenerBound) return;
  tfListenerBound = true;
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
    volumeSeries.setData([]);
    updateChartLabel();
    poll(true); // don't wait for the next tick — the click should feel instant
  });
}

// The spread used to be visible only while the order book was expanded,
// which is the one number you want at a glance on a market-making desk.
// It lives in the chart label now, book open or not.
function updateChartLabel() {
  let live = "";
  if (lastOrderbook && lastOrderbook.mid > 0) {
    const bps = ((lastOrderbook.best_ask - lastOrderbook.best_bid) / lastOrderbook.mid) * 10000;
    live = ` &middot; mid <b>${fmtPrice(lastOrderbook.mid)}</b> &middot; ${fmt(bps, 1)} bps`;
  }
  document.getElementById("chart-label").innerHTML =
    `<b>${currentSymbol || "—"}</b> &middot; ${currentInterval}${live}`;
}
let currentSymbol = "";

// The kline adapter only re-fetches from Bitunix every ~20s and caches
// between polls, so most ~1.5s polls carry byte-identical candles. Calling
// setData anyway is not just wasted work: setData is a full series reset,
// and doing it under the user's cursor is what makes a chart fight back
// while you pan through history. A cheap signature skips the no-op case.
let lastCandleSig = "";
let lastBarsMeta = null;

function renderCandles(klines) {
  // null means the server confirmed our history is current and sent none.
  if (klines === null || klines === undefined || klines.length === 0) return;
  const last = klines[klines.length - 1];
  const sig = `${klines.length}|${klines[0].time}|${last.time}|${last.close}|${last.high}|${last.low}|${last.volume}`;
  if (sig === lastCandleSig) return;
  lastCandleSig = sig;

  // Now that history runs to thousands of bars, replacing the whole series
  // to move one candle is the wrong tool: update() mutates in place.
  //
  // Measured, not assumed: setData does NOT reset the viewport — with the
  // range held at logical 239..412, setData returned it unchanged. So this
  // is a cost argument, not a scroll-position one. The viewport does shift
  // when deeper pagination prepends bars, because logical indices are
  // positional, but that settles once the history has filled in.
  const bar = (k) => ({ time: k.time, open: k.open, high: k.high, low: k.low, close: k.close });
  const volBar = (k) => ({
    time: k.time,
    value: k.volume || 0,
    color: k.close >= k.open ? BULL_VOL_COLOR : BEAR_VOL_COLOR,
  });
  const meta = { len: klines.length, first: klines[0].time, last: last.time };
  const sameTail = lastBarsMeta && lastBarsMeta.first === meta.first;
  const inPlace = sameTail && meta.len === lastBarsMeta.len && meta.last === lastBarsMeta.last;
  const appended = sameTail && meta.len === lastBarsMeta.len + 1 && meta.last > lastBarsMeta.last;

  if (inPlace) {
    candleSeries.update(bar(last));
    volumeSeries.update(volBar(last));
  } else if (appended) {
    // Finalise the bar that just closed before adding the new one; update()
    // requires non-decreasing times, so the older one has to go first.
    candleSeries.update(bar(klines[klines.length - 2]));
    candleSeries.update(bar(last));
    volumeSeries.update(volBar(klines[klines.length - 2]));
    volumeSeries.update(volBar(last));
  } else {
    // History changed shape — deeper pagination arrived, or the bot or
    // interval changed. A full reset is correct here.
    candleSeries.setData(klines.map(bar));
    volumeSeries.setData(klines.map(volBar));
  }
  lastBarsMeta = meta;
  // Markers anchor to their candle's high/low, so the primitive needs the
  // bars keyed by time.
  fillMarkers.setCandles(new Map(klines.map((k) => [k.time, { high: k.high, low: k.low }])));
}

// --- Price lines: where the bot actually sits ---
// The book shows the market; these show *us* in it. Entry price answers
// "where am I", resting quotes answer "where are my orders" — the two
// questions a market-making operator asks a chart.
// Rendering is delegated to the PriceTags primitive: the built-in
// createPriceLine has no collision handling, and a market maker's orders sit
// a few ticks apart, which is exactly when the built-in labels stack on top
// of each other. Same data as before — side, size, price — only the drawing
// changed.
let lastLineSig = "";
const MAX_QUOTE_LINES_PER_SIDE = 8;

function renderPriceLines(venue) {
  const quotes = venue.quotes || [];
  const positions = venue.positions || [];
  const sig = JSON.stringify([
    quotes.map((q) => [q.side, q.price, q.size]),
    positions.map((p) => [p.side, p.entry_price]),
  ]);
  if (sig === lastLineSig) return; // rebuilding every 1.5s makes tags flicker
  lastLineSig = sig;

  const tags = [];

  for (const p of positions) {
    if (!p.entry_price) continue;
    tags.push({
      price: p.entry_price,
      priceText: fmtPrice(p.entry_price),
      label: p.side, // LONG / SHORT
      color: p.side === "LONG" ? BULL_COLOR : BEAR_COLOR,
      kind: "entry",
    });
  }

  // A ladder bot can rest dozens of orders; drawing them all turns the
  // chart into a comb. Keep the ones nearest the mid — the far end of a
  // ladder is not what you are watching for.
  const mid = venue.orderbook && venue.orderbook.mid > 0 ? venue.orderbook.mid : null;
  const bySide = { buy: [], sell: [] };
  for (const q of quotes) if (bySide[q.side]) bySide[q.side].push(q);
  for (const side of ["buy", "sell"]) {
    let list = bySide[side];
    if (mid !== null) {
      list = list.slice().sort((a, b) => Math.abs(a.price - mid) - Math.abs(b.price - mid));
    }
    for (const q of list.slice(0, MAX_QUOTE_LINES_PER_SIDE)) {
      tags.push({
        price: q.price,
        priceText: fmtPrice(q.price),
        label: q.size ? `${side} ${fmt(q.size, 3)}` : side,
        color: side === "buy" ? BULL_COLOR : BEAR_COLOR,
        kind: "order",
      });
    }
  }

  priceTags.setTags(tags);
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

// Price precision has to follow the instrument, not a constant. Now that any
// discovered bot can appear, a hard-coded 2 decimals renders a 0.0042 asset
// as "0.00" — every price on screen identical and every spread zero.
//
// The instrument's own book is the authority: the number of decimals the
// venue quotes in *is* its tick precision. Guessing from magnitude instead
// would print SOL at 73.68 as "73.680", padding a digit the venue does not
// have. The magnitude rule stays only as a fallback for the first frames,
// before any book has arrived.
let symbolDecimals = null;

function decimalsOf(v) {
  const s = String(v);
  const dot = s.indexOf(".");
  return dot < 0 ? 0 : s.length - dot - 1;
}

function updatePriceDecimals(ob) {
  if (!ob || !ob.bids || !ob.bids.length) return;
  let d = 0;
  for (const l of ob.bids.slice(0, 20)) d = Math.max(d, decimalsOf(l.price));
  for (const l of ob.asks.slice(0, 20)) d = Math.max(d, decimalsOf(l.price));
  symbolDecimals = d;
}

function fmtPrice(p) {
  if (p === null || p === undefined) return "—";
  if (symbolDecimals !== null) return Number(p).toFixed(symbolDecimals);
  const v = Math.abs(Number(p) || 0);
  return Number(p).toFixed(v >= 1 ? 2 : v >= 0.01 ? 5 : 7);
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
        <div class="row"><span class="label">entry</span><span class="val">${fmtPrice(p.entry_price)}</span></div>
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
        <div class="row" title="Capital locked by resting orders. Leaving this out is what made the NAV swing with quoting activity rather than with net assets."><span class="label">frozen<small>orders</small></span><span class="val">${fmt(a.frozen, 2)}</span></div>
        <div class="row"><span class="label">margin used</span><span class="val">${fmt(a.margin_used, 2)}</span></div>
        <div class="row"><span class="label">uPnL</span><span class="val ${pnlCls}">${fmt(a.unrealised_pnl, 2)}</span></div>
        <div class="row" title="available + frozen + margin + uPnL, in the margin currency. Scope: ${a.equity_scope || "—"}"><span class="label">equity (nav)<small>${a.equity_scope || ""}</small></span><span class="val">${fmt(a.equity, 2)}</span></div>
        ${
          a.account_equity_total !== null && a.account_equity_total !== undefined
            ? `<div class="row" title="Every currency this venue account holds, valued in USD. Spans products this bot has nothing to do with, so it is reported apart from the NAV above rather than folded into it."><span class="label">account total<small>all ccy</small></span><span class="val">${fmt(a.account_equity_total, 2)}</span></div>`
            : ""
        }
      </div>
    `);
  } else {
    rows.push(`<div class="account-block empty-note">account margin unavailable</div>`);
  }

  body.innerHTML = rows.join("");
}

// --- Cross-venue basis ---
// Independent of which bot is selected in the chart: a two-leg MM bot (quote
// on one venue, hedge/reference on another) can drift from its own reference
// without either leg individually looking wrong, so this reads from every
// discovered pair, not the active one.
function sparklineSvg(values, w = 60, h = 16) {
  if (!values || values.length < 2) return "";
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const step = w / (values.length - 1);
  const pts = values
    .map((v, i) => `${(i * step).toFixed(1)},${(h - ((v - min) / range) * h).toFixed(1)}`)
    .join(" ");
  return `<svg width="${w}" height="${h}" class="basis-spark"><polyline points="${pts}" fill="none" stroke="currentColor" stroke-width="1.2"/></svg>`;
}

function renderBasis(basis) {
  const body = document.getElementById("basis-body");
  // Back-compat with the older shape (a bare array of pairs), so a stale
  // cached bundle talking to a new server doesn't blank the panel.
  const pairs = Array.isArray(basis) ? basis : (basis && basis.pairs) || [];
  const stale = (basis && basis.stale) || [];

  // A leg dropped for being too old is named, not silently omitted: the
  // whole point is that a frozen mid is invisible unless something says so.
  const staleRows = stale
    .map(
      (s) =>
        `<div class="row basis-row basis-stale">
           <span class="label">${s.key}</span>
           <span class="val">${s.age_s === null ? "no ts" : fmtDuration(s.age_s) + " old"}</span>
         </div>`
    )
    .join("");

  if (pairs.length === 0) {
    body.innerHTML =
      `<div class="empty-note">need 2 legs on different venues with a fresh mid</div>` + staleRows;
    return;
  }

  body.innerHTML =
    pairs
      .map((p) => {
        const cls = p.bps >= 0 ? "pnl-pos" : "pnl-neg";
        // The two mids are not sampled at the same instant (one bot writes
        // its book far more often than the other), and on a fast asset that
        // skew is worth bps of its own — the same order as the basis being
        // measured. Quoting the gap without it would be a number with no
        // protocol behind it.
        const skew =
          p.skew_s >= 1 ? `<small>±${fmt(p.skew_s, 0)}s skew</small>` : "";
        return `<div class="row basis-row">
        <span class="label">${p.label}</span>
        <span class="val ${cls}">${sparklineSvg(p.history)}${fmt(p.bps, 1)} bps${skew}</span>
      </div>`;
      })
      .join("") + staleRows;
}

// --- Incident timeline ---
// State transitions the server observed on its own background clock, not on
// this browser's poll loop — a halt that happened while no tab was open
// still appears here, which the live/warn/dead dot alone can never show.
let lastIncidentsSig = "";

function renderIncidents(events, serverTs) {
  const body = document.getElementById("incidents-body");
  if (!events || events.length === 0) {
    body.innerHTML = `<div class="empty-note">no transitions observed yet</div>`;
    lastIncidentsSig = "";
    return;
  }
  const sig = events.map((e) => `${e.bot}|${e.ts}|${e.to}`).join(",");
  if (sig === lastIncidentsSig) return;
  lastIncidentsSig = sig;

  const cls = (to) => (to === "dead" ? "pnl-neg" : to === "live" ? "pnl-pos" : "");
  body.innerHTML = events
    .map((e) => {
      const ageS = Math.max(0, serverTs - e.ts);
      return `<div class="row incident-row">
        <span class="label">${e.label}</span>
        <span class="val ${cls(e.to)}">${e.from} &rarr; ${e.to}<small>${fmtDuration(ageS)} ago</small></span>
      </div>`;
    })
    .join("");
}

// --- Bot selection ---
// null means "let the server pick the freshest", which is what a first load
// wants: land on whatever is actually running rather than on an alphabetical
// first entry that may have been dead for days.
let currentBotKey = null;
// null = follow the running session. Set only by an explicit pick in the
// session dropdown, and cleared when switching bots — session #3 of one bot
// has nothing to do with session #3 of another.
let currentSession = null;
let lastBotsSig = "";

function resetChartState() {
  symbolDecimals = null; // a new instrument has a different tick
  seenFillTimes = new Set();
  fillBuckets = new Map();
  fillMarkers.setMarkers([]);
  candleSeries.setData([]);
  volumeSeries.setData([]);
  curveSeries.setData([]);
  deltaSeries.setData([]);
  volumeCurveSeries.setData([]);
  navSeries.setData([]);
  lastCandleSig = "";
  lastLineSig = "";
  lastCurveSig = "";
  lastVolumeCurveSig = "";
  lastNavSig = "";
  priceTags.setTags([]);
}

function botStateClass(ageS) {
  if (ageS === null) return "";
  if (ageS * 1000 > BOT_DEAD_MS) return "dead";
  if (ageS * 1000 > BOT_STALE_MS) return "warn";
  return "live";
}

// One-glance portfolio row: net position across a bot's legs (hedge mode
// nets long+short into a single signed figure) and its combined uPnL. Only
// meaningful while the bot is alive — a dead bot's last-written position is
// a fossil, not a reading, so the caller passes `dead` and this refuses to
// print it, same refusal as renderDeadBot for the selected bot.
const side_ = (qty) => (qty > 0 ? "long" : qty < 0 ? "short" : "flat");

function botSummaryLine(bot, dead) {
  if (dead) return `<span class="empty-note" style="font-size:10.5px;">stopped</span>`;

  // Session PnL for the row: the realised figure when it can be defended,
  // otherwise the cash-flow one, which holds in hedge mode. Never both, and
  // never a blank where a leg is trading — the label says which is shown.
  const sessionPnl =
    !bot.pnl_unreliable && bot.realised_net !== null && bot.realised_net !== undefined
      ? { v: bot.realised_net, tag: "" }
      : bot.cash_pnl !== null && bot.cash_pnl !== undefined
      ? { v: bot.cash_pnl, tag: "<small>cash</small>" }
      : null;
  const pnlSpan = sessionPnl
    ? `<span class="val ${sessionPnl.v >= 0 ? "pnl-pos" : "pnl-neg"}">${fmt(sessionPnl.v, 4)}${sessionPnl.tag}</span>`
    : "";

  const positions = bot.positions || [];
  if (positions.length === 0) {
    return `<span class="label">flat</span>${pnlSpan}`;
  }

  let netQty = 0;
  let posUpnl = 0;
  let hasPosUpnl = false;
  for (const p of positions) {
    netQty += (p.side === "LONG" ? 1 : -1) * (p.qty_base || 0);
    if (p.unrealised_pnl !== null && p.unrealised_pnl !== undefined) {
      posUpnl += p.unrealised_pnl;
      hasPosUpnl = true;
    }
  }
  // One source, not two added together. The account's cross uPnL and the
  // position's own uPnL are the same money seen from two places — summing
  // them double-counted it (visible on OKX: +2.27 account against -0.57
  // position). The position's figure is preferred: it is scoped to the
  // symbol this row is about, while the account's spans every product on
  // that venue.
  const upnl = hasPosUpnl ? posUpnl : bot.account_upnl;
  const showUpnl = upnl !== null && upnl !== undefined;
  const pnlCls = showUpnl && upnl >= 0 ? "pnl-pos" : "pnl-neg";
  return (
    `<span class="pos-side-label ${side_(netQty)}">${side_(netQty)}</span>` +
    `<span class="val">${fmt(Math.abs(netQty), 3)}</span>` +
    (showUpnl ? `<span class="val ${pnlCls}" title="unrealised">${fmt(upnl, 4)}</span>` : "") +
    pnlSpan
  );
}

function renderBotList(bots, serverTs, selectedKey) {
  const body = document.getElementById("bots-body");
  const countEl = document.getElementById("bots-count");
  if (!bots || bots.length === 0) {
    countEl.textContent = "—";
    body.innerHTML = `<div class="empty-note">no bot found</div>`;
    lastBotsSig = "";
    return;
  }

  const live = bots.filter(
    (b) => b.source_ts && (serverTs - b.source_ts) * 1000 <= BOT_STALE_MS
  ).length;
  countEl.textContent = `${live} live / ${bots.length}`;

  // Rebuilding this list every 1.5s would kill hover and any text selection
  // in it, so only touch the DOM when something actually changed. Ages are
  // bucketed to the second for the same reason.
  const sig = bots
    .map(
      (b) =>
        `${b.key}|${b.source_ts ? Math.round(serverTs - b.source_ts) : "x"}` +
        `|${JSON.stringify(b.positions || [])}|${b.realised_net}|${b.account_upnl}` +
        `|${b.cash_pnl}|${b.session_index}`
    )
    .join(",") + `|${selectedKey}`;
  if (sig === lastBotsSig) return;
  lastBotsSig = sig;

  body.innerHTML = bots
    .map((b) => {
      const ageS = b.source_ts ? Math.max(0, serverTs - b.source_ts) : null;
      const cls = botStateClass(ageS);
      const active = b.key === selectedKey ? " active" : "";
      return `<div class="bot-row ${cls}${active}" data-bot="${b.key}">
        <div class="top-line">
          <span class="dot"></span>
          <span class="name">${b.label}</span>
          <span class="age">${ageS === null ? "—" : fmtDuration(ageS)}</span>
        </div>
        <div class="bottom-line">${botSummaryLine(b, cls === "dead")}</div>
      </div>`;
    })
    .join("");
}

document.getElementById("bots-body").addEventListener("click", (e) => {
  const row = e.target.closest(".bot-row");
  if (!row || row.dataset.bot === currentBotKey) return;
  currentBotKey = row.dataset.bot;

  // Optimistic: a /snapshot round trip through a real SSH tunnel can take
  // several seconds, well past this being the next poll tick — waiting for
  // that response to confirm the switch before moving the highlight made a
  // slow *link* look like the click hadn't registered, or worse, like it
  // had silently reverted to the old bot. Reflect the user's choice in the
  // list the instant they make it; renderBotList's own render on the next
  // successful poll just re-confirms the same thing once real data lands.
  document.querySelectorAll("#bots-body .bot-row.active").forEach((r) => r.classList.remove("active"));
  row.classList.add("active");

  resetChartState();
  lastBotsSig = "";
  currentSession = null; // session numbering is per bot
  lastSessionSig = "";
  poll(true); // supersedes any request still in flight for the old bot
});

document.getElementById("session-select").addEventListener("change", (e) => {
  // Picking the newest (running) session goes back to following it, so it
  // keeps updating instead of freezing on the index it had when picked.
  currentSession = e.target.selectedIndex === 0 ? null : e.target.value;
  resetChartState();
  poll(true);
});

function fmtDuration(s) {
  if (!s || s <= 0) return "—";
  if (s < 90) return `${Math.round(s)}s`;
  if (s < 5400) return `${Math.round(s / 60)}m`;
  if (s < 172800) return `${(s / 3600).toFixed(1)}h`;
  return `${(s / 86400).toFixed(1)}d`;
}

function fmtClock(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

// Which run is being measured, and which others can be looked at. Rebuilt
// only when the set of sessions actually changes, so the dropdown doesn't
// close itself under the operator's cursor on every 1.5s poll.
let lastSessionSig = "";
function renderSessionBar(sessions, selected) {
  const sel = document.getElementById("session-select");
  const note = document.getElementById("session-note");
  const list = sessions || [];

  const sig = list.map((s) => `${s.index}|${s.start_ts}|${s.end_ts}`).join(",") +
    `|${selected ? selected.index : ""}`;
  if (sig !== lastSessionSig) {
    lastSessionSig = sig;
    sel.innerHTML = list
      .slice()
      .reverse()
      .map((s) => {
        const d = new Date(s.start_ts * 1000);
        const day = `${String(d.getDate()).padStart(2, "0")}/${String(d.getMonth() + 1).padStart(2, "0")}`;
        const label = s.is_current
          ? `#${s.index} · running · started ${day} ${fmtClock(s.start_ts)}`
          : `#${s.index} · ${day} ${fmtClock(s.start_ts)}→${fmtClock(s.end_ts)} · ${fmtDuration(s.end_ts - s.start_ts)}`;
        return `<option value="${s.index}"${selected && s.index === selected.index ? " selected" : ""}>${label}</option>`;
      })
      .join("");
    sel.style.display = list.length ? "" : "none";
  }

  if (!selected) {
    note.textContent = "";
    return;
  }
  // A session whose start could only be lower-bounded by its first fill is
  // not the same claim as one whose start was watched happening. Say which.
  if (selected.start_source === "first fill since last session") {
    note.textContent = "start≈1st fill";
    note.title =
      "This session's start is the first fill recorded after the previous session ended — a lower bound on the real process start, which was not observed.";
  } else if (selected.clean_exit === false) {
    note.textContent = "unclean exit";
    note.title = "The tracker recorded this session ending without a clean shutdown.";
  } else {
    note.textContent = "";
    note.title = "";
  }
}

function renderStats(stats, session) {
  const body = document.getElementById("stats-body");
  const windowEl = document.getElementById("stats-window");

  if (!stats || stats.n_fills === 0) {
    windowEl.textContent = session ? fmtDuration(session.elapsed_s) : "—";
    body.innerHTML = `<div class="empty-note">${
      session && session.is_current ? "session running, no fill yet" : "no fills in window"
    }</div>`;
    return;
  }

  // The header states the window these numbers actually cover. Without it
  // every figure below invites the reader to assume "since I started
  // watching", which is never what it means. That window is now one run of
  // the bot — the thing an operator actually reasons about — instead of
  // "the last N fills", which moved under the reader's feet and spliced
  // several runs together whenever a session was short.
  windowEl.textContent = session
    ? `session #${session.index} · ${fmtDuration(session.elapsed_s)}${session.is_current ? " · running" : ""}`
    : `last ${stats.n_fills} fills · ${fmtDuration(stats.span_s)}`;

  // Withheld on one-sided flow, where it would measure price drift rather
  // than captured spread — the dash carries the reason on hover.
  const capture =
    stats.capture_bps === null || stats.capture_bps === undefined
      ? `<span title="${stats.capture_unavailable || "not enough two-sided flow"}">—</span>`
      : `${fmt(stats.capture_bps, 1)} bps`;

  // Everything above the divider is a direct count or sum over the fills:
  // true whatever else is broken. Everything below is a reconstruction that
  // assumes the ledger is complete and the account nets its position.
  const measured = `
    <div class="row"><span class="label">fills</span><span class="val">${stats.n_fills}<small>${stats.n_buys}B / ${stats.n_sells}S</small></span></div>
    <div class="row"><span class="label">rate</span><span class="val">${fmt(stats.fills_per_hour, 1)}<small>/h</small></span></div>
    <div class="row"><span class="label">volume</span><span class="val">${fmt(stats.volume_quote, 2)}</span></div>
    <div class="row"><span class="label">fees paid</span><span class="val pnl-neg">-${fmt(stats.fees, 4)}</span></div>
    <div class="row"><span class="label">capture</span><span class="val">${capture}</span></div>
  `;

  // Independent of FIFO lot matching, so it is shown even when the realised
  // figure below is refused — on a hedge-mode leg it is the only answer to
  // "how did this session go" that can be given honestly. Marked to market
  // on whatever inventory the session still carries, hence the label: this
  // is not a realised number and must not be read as one.
  let cashRow = "";
  if (stats.cash_pnl !== null && stats.cash_pnl !== undefined) {
    const cashCls = stats.cash_pnl >= 0 ? "pnl-pos" : "pnl-neg";
    const openBasis =
      stats.cash_pnl_basis === "ledger"
        ? "opening inventory from the bot's own record on the session's first fill"
        : stats.cash_pnl_basis === "replay"
        ? "opening inventory rebuilt by replaying earlier fills — the ledger records none"
        : "window opens flat by construction";
    cashRow = (
      `<div class="row stat-net" title="Cash in from sells minus cash out on buys, plus closing inventory marked at the last mid, minus the same for the opening inventory, minus fees. No lot matching, so it holds in hedge mode. Includes mark-to-market on unclosed inventory — ${openBasis}.">
         <span class="label">session pnl<small>cash</small></span>
         <span class="val ${cashCls}">${fmt(stats.cash_pnl, 4)}</span>
       </div>`
    );
  }

  if (stats.pnl_unreliable) {
    // A PnL that cannot be defended is not shown as a number. Printing it
    // greyed out would still leave a figure on screen to be read and
    // believed, which is exactly how the wrong one got trusted.
    body.innerHTML =
      measured +
      cashRow +
      `<div class="stat-net pnl-blocked">
         <div class="pnl-blocked-title">realised pnl unavailable</div>
         <div class="pnl-blocked-why">${stats.pnl_unreliable}</div>
       </div>`;
    return;
  }

  const netCls = stats.realised_net >= 0 ? "pnl-pos" : "pnl-neg";
  // A finished session shows what it *ended* holding. Showing the current
  // inventory under a header naming last night's run would be the same
  // category of error this panel exists to avoid.
  const inv =
    session && !session.is_current ? stats.closing_inventory_base : stats.inventory_base;
  body.innerHTML =
    measured +
    cashRow +
    `
    <div class="row"><span class="label">inventory</span><span class="val">${fmt(inv, 3)}</span></div>
    <div class="row"><span class="label">realised gross</span><span class="val">${fmt(stats.realised_gross, 4)}</span></div>
    <div class="row stat-net"><span class="label">realised net</span><span class="val ${netCls}">${fmt(stats.realised_net, 4)}</span></div>
  ` +
    // Shown, but not passed off as checked. Distinct from the refusal
    // above: there is no evidence this figure is wrong, only no way to
    // prove it right.
    (stats.pnl_unverified
      ? `<div class="pnl-unverified" title="${stats.pnl_unverified}">unverified — no position published to check the replay against</div>`
      : "");
}

// --- Curve (cumulative realised PnL) & Delta (running inventory) ---
// Same replay as the Performance panel, drawn as a series instead of
// collapsed to a total. Baseline series so the 0 line reads at a glance —
// above is green, below is red, exactly like the number it summarises.
function makeMiniChart(elId) {
  const el = document.getElementById(elId);
  const c = LightweightCharts.createChart(el, {
    layout: { background: { color: "transparent" }, textColor: token("--chart-text"), attributionLogo: false },
    grid: { vertLines: { visible: false }, horzLines: { visible: false } },
    timeScale: { visible: false },
    rightPriceScale: { visible: false },
    handleScroll: false,
    handleScale: false,
    crosshair: { horzLine: { visible: false }, vertLine: { visible: false } },
  });
  const series = c.addSeries(LightweightCharts.BaselineSeries, {
    baseValue: { type: "price", price: 0 },
    topLineColor: BULL_COLOR,
    topFillColor1: hexToRgba(BULL_COLOR, 0.28),
    topFillColor2: hexToRgba(BULL_COLOR, 0.03),
    bottomLineColor: BEAR_COLOR,
    bottomFillColor1: hexToRgba(BEAR_COLOR, 0.03),
    bottomFillColor2: hexToRgba(BEAR_COLOR, 0.28),
    lineWidth: 1,
  });
  new ResizeObserver((entries) => {
    const { width, height } = entries[0].contentRect;
    c.resize(width, height);
  }).observe(el);
  return series;
}
const curveSeries = makeMiniChart("curve-chart");
const deltaSeries = makeMiniChart("delta-chart");
const volumeCurveSeries = makeMiniChart("volume-curve-chart");
const navSeries = makeMiniChart("nav-chart");
let lastCurveSig = "";
let lastVolumeCurveSig = "";
let lastNavSig = "";

// Same same-ts collapse the Curve/Delta blocks need below: a series requires
// strictly increasing time, and fills can share a timestamp.
function _byTime(points) {
  const m = new Map();
  for (const p of points) m.set(p.ts, p);
  return [...m.entries()].sort((a, b) => a[0] - b[0]);
}

function renderVolumeCurve(curve) {
  // Independent of pnl_unreliable on purpose — see the HTML comment above
  // #volume-curve-block: cumulative traded notional doesn't depend on FIFO
  // lot matching, so a window whose realised PnL can't be trusted still has
  // an honest volume curve.
  const valEl = document.getElementById("volume-curve-val");
  const emptyEl = document.getElementById("volume-curve-empty");
  const block = document.getElementById("volume-curve-block");

  if (!curve || curve.length === 0) {
    block.style.display = "none";
    emptyEl.style.display = "";
    valEl.textContent = "—";
    lastVolumeCurveSig = "";
    return;
  }

  const last = curve[curve.length - 1];
  const sig = `${curve.length}|${last.ts}|${last.cum_volume_quote}`;
  if (sig === lastVolumeCurveSig) return;
  lastVolumeCurveSig = sig;

  block.style.display = "";
  emptyEl.style.display = "none";

  const points = _byTime(curve);
  volumeCurveSeries.setData(points.map(([ts, p]) => ({ time: ts, value: p.cum_volume_quote })));
  valEl.textContent = fmt(last.cum_volume_quote, 2);
}

function renderCurve(curve) {
  renderVolumeCurve(curve);

  const panel = document.getElementById("curve-panel");
  const grid = document.getElementById("curve-panel-grid");
  const emptyEl = document.getElementById("curve-empty");
  const blocks = panel.querySelectorAll(".mini-chart-block:not(#volume-curve-block)");
  const curveVal = document.getElementById("curve-val");
  const curveDdVal = document.getElementById("curve-dd-val");
  const deltaVal = document.getElementById("delta-val");

  if (!curve || curve.length === 0) {
    blocks.forEach((b) => (b.style.display = "none"));
    // No curve data at all (not even for Volume — renderVolumeCurve above
    // already hid its block too), so there's nothing left to span the row.
    grid.classList.remove("only-volume");
    emptyEl.style.display = "";
    emptyEl.textContent = "no fills yet";
    curveVal.textContent = "—";
    curveDdVal.textContent = "dd —";
    deltaVal.textContent = "—";
    lastCurveSig = "";
    return;
  }

  const last = curve[curve.length - 1];
  if (last.pnl_unreliable) {
    // Same refusal as the Performance panel, for the same reason: a replay
    // that can't defend its total can't defend any point on its curve
    // either — plotting it anyway would just move the lie into a chart.
    // Drawdown is derived from realised_net, so it's exactly as fictional
    // as the total on a window flagged unreliable — same refusal.
    blocks.forEach((b) => (b.style.display = "none"));
    grid.classList.add("only-volume"); // Volume is the only block left, let it take the row
    emptyEl.style.display = "";
    emptyEl.innerHTML = `<div class="pnl-blocked-title">unavailable</div><div class="pnl-blocked-why">${last.pnl_unreliable}</div>`;
    curveVal.textContent = "—";
    curveDdVal.textContent = "dd —";
    deltaVal.textContent = "—";
    lastCurveSig = "";
    return;
  }

  const sig = `${curve.length}|${last.ts}|${last.realised_net}|${last.inventory_base}|${last.max_drawdown}`;
  if (sig === lastCurveSig) return;
  lastCurveSig = sig;

  blocks.forEach((b) => (b.style.display = ""));
  grid.classList.remove("only-volume");
  emptyEl.style.display = "none";

  const points = _byTime(curve);

  curveSeries.setData(points.map(([ts, p]) => ({ time: ts, value: p.realised_net })));
  deltaSeries.setData(points.map(([ts, p]) => ({ time: ts, value: p.inventory_base })));

  curveVal.textContent = fmt(last.realised_net, 4);
  curveVal.className = `val ${last.realised_net >= 0 ? "pnl-pos" : "pnl-neg"}`;
  // Drawdown is a magnitude (always >= 0); shown negated ("dd -3.20") since
  // that's how far below the peak the curve currently sits, not a gain.
  curveDdVal.textContent = `dd ${last.max_drawdown > 0 ? "-" : ""}${fmt(last.max_drawdown, 4)}`;
  deltaVal.textContent = fmt(last.inventory_base, 3);
  deltaVal.className = `val ${last.inventory_base >= 0 ? "pnl-pos" : "pnl-neg"}`;
}

// --- Markout ---
// Where the mid went after each fill, per horizon. The one panel that can
// tell a spread that was captured from one that was handed straight back.
// Bars diverge from a centre line: right = kept, left = given back.
function renderMarkout(mo) {
  const body = document.getElementById("markout-body");
  const note = document.getElementById("markout-note");
  const points = (mo && mo.points) || [];
  const sampled = points.filter((p) => p.n > 0);

  if (sampled.length === 0) {
    note.textContent = "—";
    body.innerHTML = `<div class="empty-note">no markout data for this session</div>`;
    return;
  }

  // n is the same for every horizon in practice, but report the largest
  // rather than assume it — a horizon the tracker hasn't reached yet (a
  // fill 10s old has no 30s mid) legitimately has fewer.
  const n = Math.max(...sampled.map((p) => p.n));
  note.textContent = `${n} fills`;
  note.title =
    mo.joined_by === "trade_id"
      ? "Matched to this bot's own fills by trade id — this venue carries more than one symbol."
      : "This venue carries a single symbol in the ledger, so every markout row on it belongs to this bot.";

  // Scale to the largest magnitude on screen, so a flat book doesn't render
  // as noise amplified to full width — but never below 1bp, or a 0.05bp
  // wobble would look like a catastrophe.
  const scale = Math.max(1, ...sampled.map((p) => Math.abs(p.bps)));

  body.innerHTML =
    points
      .map((p) => {
        if (p.n === 0) {
          return `<div class="mo-row"><span class="h">${p.horizon}</span>
            <span class="mo-track"></span><span class="mo-val">—</span>
            <span class="mo-sides">not sampled</span></div>`;
        }
        const w = Math.min(50, (Math.abs(p.bps) / scale) * 50);
        const cls = p.bps >= 0 ? "pos" : "neg";
        const side = (v) => (v === null || v === undefined ? "—" : fmt(v, 1));
        return `<div class="mo-row" title="mean ${fmt(p.bps, 2)} bps, median ${fmt(
          p.median_bps,
          2
        )} bps over ${p.n} fills">
          <span class="h">${p.horizon}</span>
          <span class="mo-track"><span class="mo-bar ${cls}" style="width:${w}%"></span></span>
          <span class="mo-val ${p.bps >= 0 ? "pnl-pos" : "pnl-neg"}">${fmt(p.bps, 2)}</span>
          <span class="mo-sides">B ${side(p.buy_bps)} / S ${side(p.sell_bps)}</span>
        </div>`;
      })
      .join("") +
    `<div class="mo-legend">bps the mid moved your way after a fill. Negative = adverse selection.
     Relative to the mid at fill, so it is unaffected by where that mid sits.</div>`;
}

function renderNav(navCurve) {
  const valEl = document.getElementById("nav-val");
  const emptyEl = document.getElementById("nav-empty");
  const block = document.querySelector("#nav-panel .mini-chart-block");

  if (!navCurve || navCurve.length === 0) {
    block.style.display = "none";
    emptyEl.style.display = "";
    valEl.textContent = "—";
    lastNavSig = "";
    return;
  }

  const last = navCurve[navCurve.length - 1];
  const sig = `${navCurve.length}|${last.ts}|${last.equity}`;
  if (sig === lastNavSig) return;
  lastNavSig = sig;

  block.style.display = "";
  emptyEl.style.display = "none";

  const points = _byTime(navCurve);
  navSeries.setData(points.map(([ts, p]) => ({ time: ts, value: p.equity })));
  valEl.textContent = fmt(last.equity, 2);

  // The window this curve actually covers. It is sampled once per poll by
  // whoever is watching — not replayed from any ledger — so it is minutes
  // long, shrinks when several tabs share the buffer, and does not exist at
  // all while nobody has the dashboard open. Stating it stops the axis from
  // being read as "the session".
  const span = last.ts - navCurve[0].ts;
  const windowEl = document.getElementById("nav-window");
  if (windowEl) {
    windowEl.textContent = `sampled ${fmtDuration(span)}`;
    windowEl.title =
      "Account equity is polled, not reconstructed: this series only covers the time a browser has been polling, and is capped at NAV_HISTORY_LEN samples.";
  }
}

// A bot past the dead threshold keeps its name and its age, and loses every
// number. The alternative — greying the last values out — still leaves a
// figure on screen to be read and believed, and a four-day-old position read
// as current is worse than no reading at all. Same reasoning as the
// unavailable-PnL block in renderStats: refuse to print, say why.
function renderDeadBot(ageS) {
  // Chart overlays are the bot's own marks; they go with the numbers.
  seenFillTimes = new Set();
  fillBuckets = new Map();
  fillMarkers.setMarkers([]);
  lastLineSig = "";
  priceTags.setTags([]);

  const dash = (label) =>
    `<div class="row"><span class="label">${label}</span><span class="val">—</span></div>`;
  const stopped = `<div class="empty-note">stopped ${fmtDuration(ageS)} ago</div>`;

  document.getElementById("position-body").innerHTML =
    dash("qty") + dash("entry") + dash("uPnL") +
    `<div class="account-block">` +
    dash("available") + dash("margin used") + dash("cross uPnL") + dash("equity (nav)") +
    `</div>` + stopped;

  // Performance is deliberately NOT dashed out here. What a stopped bot
  // asserts about *now* — its book, its position, its equity — is a fossil
  // and gets refused. What it did during a session that has since ended is
  // a settled historical fact, computed from a fill ledger that doesn't go
  // stale, and it is the main thing an operator opens this dashboard for
  // the morning after. The caller keeps rendering stats/curve/session bar.

  renderBook(null);
  renderNav(null);
}

function renderBook(ob, quotes) {
  const asksEl = document.getElementById("book-asks");
  const bidsEl = document.getElementById("book-bids");
  const asksOffEl = document.getElementById("book-asks-off");
  const bidsOffEl = document.getElementById("book-bids-off");
  const spreadEl = document.getElementById("book-spread");
  const imbFillEl = document.getElementById("imb-fill-bid");
  const imbLabelEl = document.getElementById("imb-label");
  if (!ob) {
    asksEl.innerHTML = "";
    bidsEl.innerHTML = "";
    asksOffEl.textContent = "";
    bidsOffEl.textContent = "";
    spreadEl.textContent = "—";
    imbFillEl.style.width = "0%";
    imbLabelEl.textContent = "—";
    return;
  }
  quotes = quotes || [];
  // Book levels come from the exchange at its own tick precision; quote
  // prices come from the bot's own order state — comparing them exactly
  // would miss a match on float noise, so match within half a tick instead.
  const tol = symbolDecimals !== null ? Math.pow(10, -symbolDecimals) / 2 : 1e-9;
  const findQuote = (side, price) => quotes.find((q) => q.side === side && Math.abs(q.price - price) <= tol);

  // The book now has its own wide column (not the narrow rail), so it can
  // afford to show more of the ladder than the old 7-level compact view.
  const depth = 15;
  // Cumulative size from the best price outward, computed before the asks
  // side is reversed for display — this is what the depth bar is keyed to
  // now (how much liquidity sits between here and the top of book), not the
  // single level's own size, which is the standard depth-ladder reading and
  // makes a real wall visible instead of just a locally tall bar.
  const withCum = (levels) => {
    let cum = 0;
    return levels.map((l) => {
      cum += l.size;
      return { ...l, cum };
    });
  };
  const asksCum = withCum(ob.asks.slice(0, depth));
  const bidsCum = withCum(ob.bids.slice(0, depth));
  const askTotal = asksCum.length ? asksCum[asksCum.length - 1].cum : 0;
  const bidTotal = bidsCum.length ? bidsCum[bidsCum.length - 1].cum : 0;
  const asks = asksCum.slice().reverse();
  const bids = bidsCum;
  const maxCum = Math.max(1e-9, askTotal, bidTotal);
  // bookSide is the quote-side vocabulary ("buy"/"sell"); side is the CSS
  // class ("bid"/"ask") — same level, two vocabularies to reconcile.
  const bookRow = (l, side, bookSide) => {
    const pct = Math.min(100, (l.cum / maxCum) * 100);
    const own = findQuote(bookSide, l.price);
    const ownTag = own ? `<span class="own-tag">●${fmt(own.size, 3)}</span>` : "";
    return `<div class="book-row ${side}${own ? " own" : ""}"><div class="depth-bar" style="width:${pct}%"></div><span>${fmtPrice(l.price)}</span><span>${fmt(l.size, 3)}${ownTag}</span></div>`;
  };
  asksEl.innerHTML = asks.map((l) => bookRow(l, "ask", "sell")).join("");
  bidsEl.innerHTML = bids.map((l) => bookRow(l, "bid", "buy")).join("");
  const spreadBps = ob.mid > 0 ? ((ob.best_ask - ob.best_bid) / ob.mid) * 10000 : 0;
  spreadEl.textContent = `spread ${fmtPrice(ob.best_ask - ob.best_bid)} (${fmt(spreadBps, 1)} bps)`;

  // Bid/ask imbalance over the same displayed depth as the ladder above —
  // whatever "depth" levels means here, the imbalance is over exactly that,
  // not some other unstated window.
  const bookTotal = bidTotal + askTotal;
  if (bookTotal > 0) {
    const bidPct = (bidTotal / bookTotal) * 100;
    imbFillEl.style.width = `${bidPct}%`;
    imbLabelEl.textContent = `${fmt(bidPct, 0)}% bid · ${fmt(100 - bidPct, 0)}% ask (top ${depth})`;
  } else {
    imbFillEl.style.width = "0%";
    imbLabelEl.textContent = "—";
  }

  // A resting quote past the visible depth still says where the bot is
  // waiting — worth one line, not worth silently dropping.
  const offDepthNote = (bookSide, visibleLevels) => {
    const off = quotes.filter(
      (q) => q.side === bookSide && !visibleLevels.some((l) => Math.abs(l.price - q.price) <= tol)
    );
    if (off.length === 0) return "";
    const shown = off.slice(0, 3).map((q) => fmtPrice(q.price));
    const more = off.length > 3 ? ` +${off.length - 3}` : "";
    return `● ${off.length} more ${bookSide} @ ${shown.join(", ")}${more}`;
  };
  asksOffEl.textContent = offDepthNote("sell", asks);
  bidsOffEl.textContent = offDepthNote("buy", bids);
}

const bookPanel = document.getElementById("book-panel");
const bookToggle = document.getElementById("book-toggle");
// Visible by default: it sits high in the rail now and is meant to be
// read at a glance, not opened on demand.
let bookVisible = true;
let lastOrderbook = null;
let lastQuotes = [];
bookToggle.addEventListener("click", () => {
  bookVisible = !bookVisible;
  bookPanel.classList.toggle("visible", bookVisible);
  bookToggle.textContent = bookVisible ? "hide" : "show";
  renderBook(bookVisible ? lastOrderbook : null, lastQuotes); // don't wait for next poll tick
});

// --- Generic panel collapse ---
// Same hide/show affordance as the order book above, generalised: any
// `.panel` can shrink to just its title bar via `<button class="panel-toggle"
// data-panel="...">` in its h2 (see index.html) and CSS's
// `.panel.collapsed > *:not(h2)`. The order book keeps its own listener
// above instead of this one, since hiding it also has to blank the book
// render (see renderBook call above) rather than just toggling a class.
// State is per panel, keyed by id, so a layout choice survives a reload.
const COLLAPSE_KEY_PREFIX = "dashboard.collapsed.";
document.querySelectorAll("button.panel-toggle[data-panel]").forEach((btn) => {
  const panelId = btn.dataset.panel;
  const panel = document.getElementById(panelId);
  if (!panel) return;
  const setCollapsed = (collapsed) => {
    panel.classList.toggle("collapsed", collapsed);
    btn.textContent = collapsed ? "show" : "hide";
  };
  setCollapsed(localStorage.getItem(COLLAPSE_KEY_PREFIX + panelId) === "1");
  btn.addEventListener("click", () => {
    const collapsed = !panel.classList.contains("collapsed");
    setCollapsed(collapsed);
    localStorage.setItem(COLLAPSE_KEY_PREFIX + panelId, collapsed ? "1" : "0");
  });
});

// --- Order book column resize ---
// Only the chart column is `1fr` in #layout's grid-template-columns; the
// handle, book and rail are all fixed/explicit widths. That means the
// side rail's left edge never moves while dragging — only the chart
// shrinks or grows to absorb the change — so the book's width can be read
// straight off the mouse position relative to the rail's stable edge,
// with no feedback loop against the chart's own size.
const layoutEl = document.getElementById("layout");
const resizeHandle = document.getElementById("book-resize-handle");
const sideEl = document.getElementById("side");
const BOOK_WIDTH_KEY = "dashboard.bookWidthPx";
const BOOK_WIDTH_MIN = 200;
const RAIL_WIDTH_KEY = "dashboard.railWidthPx";
const RAIL_WIDTH_MIN = 200;
// Layout padding (2x) + chart min-width + the two 10px handle columns +
// the 4 gaps between the 5 columns — everything in #layout that is
// neither the book column nor the rail column.
const FIXED_LAYOUT_OVERHEAD = 20 + 300 + 20 + 40;

// Both clamps are declared here, above the first call, and exactly once.
// They used to sit in two separate blocks and `clampBookWidth` ended up
// defined twice; hoisting made the *later* definition win at the earlier
// call site, where its `const` dependencies were still in the temporal
// dead zone. That threw a ReferenceError during load for anyone with a
// saved book width, and since an uncaught throw abandons the rest of the
// script, every feature below this line silently stopped being wired up.
function currentBookWidthPx() {
  const v = parseFloat(getComputedStyle(layoutEl).getPropertyValue("--book-w"));
  return Number.isNaN(v) ? 400 : v;
}
function currentRailWidthPx() {
  const v = parseFloat(getComputedStyle(layoutEl).getPropertyValue("--rail-w"));
  return Number.isNaN(v) ? 260 : v;
}
function clampBookWidth(px) {
  const maxPx = Math.max(BOOK_WIDTH_MIN, layoutEl.clientWidth - FIXED_LAYOUT_OVERHEAD - currentRailWidthPx());
  return Math.min(Math.max(px, BOOK_WIDTH_MIN), maxPx);
}
function clampRailWidth(px) {
  const maxPx = Math.max(RAIL_WIDTH_MIN, layoutEl.clientWidth - FIXED_LAYOUT_OVERHEAD - currentBookWidthPx());
  return Math.min(Math.max(px, RAIL_WIDTH_MIN), maxPx);
}
function setRailWidth(px, persist) {
  const clamped = clampRailWidth(px);
  layoutEl.style.setProperty("--rail-w", `${clamped}px`);
  if (persist) localStorage.setItem(RAIL_WIDTH_KEY, String(clamped));
}

function setBookWidth(px, persist) {
  const clamped = clampBookWidth(px);
  layoutEl.style.setProperty("--book-w", `${clamped}px`);
  if (persist) localStorage.setItem(BOOK_WIDTH_KEY, String(clamped));
}

const savedBookWidth = parseFloat(localStorage.getItem(BOOK_WIDTH_KEY));
if (!Number.isNaN(savedBookWidth)) setBookWidth(savedBookWidth, false);

// Pointer Events + setPointerCapture instead of mousedown/mousemove/mouseup
// on window: the handle is a thin 10px hit target, and a fast real-world
// drag (trackpad flick, not a slow mouse crawl) can move the cursor past it
// between two move samples. Plain mouse listeners on window still receive
// those moves, but capturing the pointer to the handle is what guarantees
// this element keeps getting every move/up event for the gesture even once
// the cursor has left its bounds — the standard fix for "drag stops working
// if you move too fast", which is exactly the failure mode a 10px handle
// invites.
let resizingBook = false;
resizeHandle.addEventListener("pointerdown", (e) => {
  resizingBook = true;
  resizeHandle.classList.add("dragging");
  resizeHandle.setPointerCapture(e.pointerId);
  document.body.style.cursor = "col-resize";
  document.body.style.userSelect = "none"; // dragging shouldn't select chart labels/text
  e.preventDefault();
});
resizeHandle.addEventListener("pointermove", (e) => {
  if (!resizingBook) return;
  // Book width = distance from the cursor to the rail's fixed left edge,
  // read fresh every move rather than assumed constant, so a window resize
  // mid-drag can't leave this stale.
  const railLeft = sideEl.getBoundingClientRect().left;
  setBookWidth(railLeft - e.clientX - 10 /* gap between book and rail */, false);
});
function endBookResize(e) {
  if (!resizingBook) return;
  resizingBook = false;
  resizeHandle.classList.remove("dragging");
  if (e && resizeHandle.hasPointerCapture(e.pointerId)) resizeHandle.releasePointerCapture(e.pointerId);
  document.body.style.cursor = "";
  document.body.style.userSelect = "";
  // Persisted only once the drag settles, not on every pixel of movement.
  const current = getComputedStyle(layoutEl).getPropertyValue("--book-w");
  if (current) localStorage.setItem(BOOK_WIDTH_KEY, current.trim());
}
resizeHandle.addEventListener("pointerup", endBookResize);
resizeHandle.addEventListener("pointercancel", endBookResize);
// A saved width from a wider window can overflow a narrower one on reload;
// re-clamp against whatever viewport actually loaded this time.
window.addEventListener("resize", () => {
  const current = parseFloat(getComputedStyle(layoutEl).getPropertyValue("--book-w"));
  if (!Number.isNaN(current)) setBookWidth(current, false);
});

// --- Side rail width resize ---
// Second handle, same mechanism as the book's, between the book and the
// rail. #layout has 5 columns (chart, handle, book, handle, rail), so each
// handle's clamp reserves the *other* variable-width column too — the
// book's max depends on how wide the rail currently is, and vice versa,
// not on a hardcoded 260px. Both clamps live above, with the constants.
const railResizeHandle = document.getElementById("rail-resize-handle");

const savedRailWidth = parseFloat(localStorage.getItem(RAIL_WIDTH_KEY));
if (!Number.isNaN(savedRailWidth)) setRailWidth(savedRailWidth, false);

let resizingRail = false;
railResizeHandle.addEventListener("pointerdown", (e) => {
  resizingRail = true;
  railResizeHandle.classList.add("dragging");
  railResizeHandle.setPointerCapture(e.pointerId);
  document.body.style.cursor = "col-resize";
  document.body.style.userSelect = "none";
  e.preventDefault();
});
railResizeHandle.addEventListener("pointermove", (e) => {
  if (!resizingRail) return;
  // Rail width = distance from the cursor to #layout's fixed right inner
  // edge, mirroring the book handle's left-edge measurement.
  const layoutRect = layoutEl.getBoundingClientRect();
  const padRight = parseFloat(getComputedStyle(layoutEl).paddingRight) || 0;
  setRailWidth(layoutRect.right - padRight - e.clientX - 10 /* gap between handle and rail */, false);
});
function endRailResize(e) {
  if (!resizingRail) return;
  resizingRail = false;
  railResizeHandle.classList.remove("dragging");
  if (e && railResizeHandle.hasPointerCapture(e.pointerId)) railResizeHandle.releasePointerCapture(e.pointerId);
  document.body.style.cursor = "";
  document.body.style.userSelect = "";
  const current = getComputedStyle(layoutEl).getPropertyValue("--rail-w");
  if (current) localStorage.setItem(RAIL_WIDTH_KEY, current.trim());
}
railResizeHandle.addEventListener("pointerup", endRailResize);
railResizeHandle.addEventListener("pointercancel", endRailResize);
window.addEventListener("resize", () => {
  const current = parseFloat(getComputedStyle(layoutEl).getPropertyValue("--rail-w"));
  if (!Number.isNaN(current)) setRailWidth(current, false);
});

// --- Per-panel size (drag the grip) ---
// Drag a panel's bottom grip to resize the panel box: the drag sets an
// explicit pixel height on that panel's body, 1:1 with the mouse, and
// because a panel is height:auto the panel itself shrinks or grows with
// it. Content that stops fitting scrolls inside the body. Double-click
// resets to the natural size.
//
// The type follows only faintly. An earlier cut scaled the text and let
// the box follow, which is backwards: shrinking a panel is how you buy
// screen space for the panel you *are* watching, and it is worthless if
// the one you shrank becomes unreadable. So the box moves freely while
// --pz stays in a narrow band around 1.
const SIZE_KEY_PREFIX = "dashboard.panelSize.";
// Fraction of the box's proportional change that reaches the type: halve
// the box and the text loses ~12%, double it and the text gains ~25%.
const TEXT_RESPONSE = 0.25;
const PZ_MIN = 0.9;
const PZ_MAX = 1.6;
const MIN_BODY_H = 26;
// The main chart pane is excluded: it already sizes itself against the
// viewport and its candles are a canvas, not type. #curve-panel, nested
// inside it, is included on its own.
const SIZE_EXCLUDE = new Set(["chart-pane"]);

// Which element inside each panel actually carries the height. Written out
// rather than inferred ("last child", "biggest child"): the panels differ
// enough — NAV's body is its chart, Curve's is three charts side by side,
// the book's is the whole bid/ask stack — that a clever rule would be a
// guess that breaks the day a panel gains a footer.
const PANEL_BODIES = {
  "bots-panel": ["bots-body"],
  "basis-panel": ["basis-body"],
  "incidents-panel": ["incidents-body"],
  "position-panel": ["position-body"],
  "stats-panel": ["stats-body"],
  "markout-panel": ["markout-body"],
  "nav-panel": ["nav-chart"],
  "curve-panel": ["curve-chart", "delta-chart", "volume-curve-chart"],
  "book-pane": ["book-panel"],
};

const clampPz = (z) => Math.min(Math.max(z, PZ_MIN), PZ_MAX);

document.querySelectorAll(".panel[id]").forEach((panel) => {
  if (SIZE_EXCLUDE.has(panel.id)) return;
  const bodies = (PANEL_BODIES[panel.id] || [])
    .map((id) => document.getElementById(id))
    .filter(Boolean);
  if (bodies.length === 0) return;

  const grip = document.createElement("div");
  grip.className = "panel-grip";
  grip.title = "Drag to resize this panel — double-click to reset";
  const readout = document.createElement("span");
  readout.className = "grip-readout";
  grip.appendChild(readout);
  panel.appendChild(grip);

  // A canvas body must not grow a scrollbar; a list body must.
  const isChart = (el) => el.classList.contains("mini-chart");
  bodies.forEach((el) => {
    if (!isChart(el)) el.style.overflowY = "auto";
  });

  // Height with nothing imposed, measured on the element that leads the
  // group. Read once per gesture rather than continuously: --pz changes
  // the natural height, so measuring mid-drag would feed the drag back
  // into itself and make the panel run away from the cursor.
  function naturalHeight() {
    const el = bodies[0];
    const prevH = el.style.height;
    const prevMax = el.style.maxHeight;
    el.style.height = "auto";
    el.style.maxHeight = "none";
    const h = el.getBoundingClientRect().height;
    el.style.height = prevH;
    el.style.maxHeight = prevMax;
    return Math.max(h, MIN_BODY_H);
  }

  function applySize(h, natural) {
    bodies.forEach((el) => {
      el.style.height = `${h}px`;
      // #incidents-body ships a max-height; an explicit height must win
      // over it or the panel would refuse to grow past that cap.
      el.style.maxHeight = "none";
    });
    panel.style.setProperty("--pz", String(clampPz(1 + (h / natural - 1) * TEXT_RESPONSE)));
  }

  function resetSize() {
    bodies.forEach((el) => {
      el.style.height = "";
      el.style.maxHeight = "";
    });
    panel.style.removeProperty("--pz");
    localStorage.removeItem(SIZE_KEY_PREFIX + panel.id);
  }

  const key = SIZE_KEY_PREFIX + panel.id;
  const saved = JSON.parse(localStorage.getItem(key) || "null");
  if (saved && saved.h > 0) {
    // --pz is restored from storage, not recomputed: at load the panel
    // still says "scanning…", so its natural height is nothing like what
    // it will be once data arrives, and a recomputed ratio would spike.
    bodies.forEach((el) => {
      el.style.height = `${saved.h}px`;
      el.style.maxHeight = "none";
    });
    panel.style.setProperty("--pz", String(clampPz(saved.pz || 1)));
  }

  let dragging = false;
  let startY = 0;
  let startH = 0;
  let natural = 0;

  grip.addEventListener("pointerdown", (e) => {
    dragging = true;
    startY = e.clientY;
    startH = bodies[0].getBoundingClientRect().height;
    natural = naturalHeight();
    grip.classList.add("dragging");
    grip.setPointerCapture(e.pointerId);
    document.body.style.cursor = "ns-resize";
    document.body.style.userSelect = "none";
    readout.textContent = `${Math.round(startH)}px`;
    e.preventDefault();
  });
  grip.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    const h = Math.max(MIN_BODY_H, startH + (e.clientY - startY));
    applySize(h, natural);
    readout.textContent = `${Math.round(h)}px`;
  });
  function endSizeDrag(e) {
    if (!dragging) return;
    dragging = false;
    grip.classList.remove("dragging");
    if (e && grip.hasPointerCapture(e.pointerId)) grip.releasePointerCapture(e.pointerId);
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
    readout.textContent = "";
    localStorage.setItem(key, JSON.stringify({
      h: Math.round(bodies[0].getBoundingClientRect().height),
      pz: parseFloat(panel.style.getPropertyValue("--pz")) || 1,
    }));
  }
  grip.addEventListener("pointerup", endSizeDrag);
  grip.addEventListener("pointercancel", endSizeDrag);
  grip.addEventListener("dblclick", resetSize);
});

// --- Freshness tracking ---
let lastGoodPollTs = 0; // local clock, for link health
// Bot age is measured server-side (server_ts - source_ts) and then extended
// locally. Subtracting the bot's timestamp from the *browser's* clock would
// report a healthy bot as dead whenever the two machines disagree, which
// for a VPS in another timezone they routinely do by seconds or more.
let botAgeAtPollS = null;
let botAgeMeasuredAt = 0;
let everConnected = false;

const statusEl = document.getElementById("status");
const statusTextEl = document.getElementById("status-text");
const statusDetailEl = document.getElementById("status-detail");

function currentBotAgeS() {
  if (botAgeAtPollS === null) return null;
  return botAgeAtPollS + (Date.now() - botAgeMeasuredAt) / 1000;
}

function renderStatus() {
  const linkAgeMs = Date.now() - lastGoodPollTs;
  const botAgeS = currentBotAgeS();
  let cls, text, detail;

  // Whether the panels still hold numbers that need a warning. A dead bot has
  // had every figure replaced by a dash already, so it needs none; a dead
  // *link* has blanked nothing, because the poll that would have blanked it is
  // the thing that failed — its stale numbers are exactly the dangerous case.
  let numbersOnScreen = true;

  if (!everConnected) {
    cls = "";
    text = "connecting";
    detail = "";
  } else if (linkAgeMs > LINK_STALE_MS) {
    // We cannot reach our own server: nothing on screen can be trusted.
    cls = "dead";
    text = "link down";
    detail = fmtDuration(linkAgeMs / 1000);
  } else if (botAgeS === null) {
    cls = "warn";
    text = "bot unknown";
    detail = "no timestamp";
  } else if (botAgeS * 1000 > BOT_DEAD_MS) {
    cls = "dead";
    text = "bot dead";
    detail = fmtDuration(botAgeS);
    numbersOnScreen = false; // renderDeadBot dashed them all out
  } else if (botAgeS * 1000 > BOT_STALE_MS) {
    cls = "warn";
    text = "bot stale";
    detail = fmtDuration(botAgeS);
  } else {
    cls = "ok";
    text = "live";
    detail = fmtDuration(botAgeS);
  }

  statusEl.className = cls;
  statusTextEl.textContent = text;
  statusDetailEl.textContent = detail;
  // Desaturate only while doubtful numbers are actually on screen, so they
  // cannot be misread as current from across the room. Greying panels that
  // already read "—" would make the dashboard look broken rather than make
  // the bot look stopped.
  document.body.classList.toggle(
    "stale",
    numbersOnScreen && (cls === "warn" || cls === "dead")
  );

  // Same clock as the rest of this function, not a second read of ob.ts —
  // the book's own timestamp and the bot's heartbeat are the same field on
  // the wire (both come from the bot's last viz-file write), so reusing
  // botAgeS here is one source of truth instead of two that could disagree.
  const bookAgeEl = document.getElementById("book-age");
  if (bookAgeEl) bookAgeEl.textContent = botAgeS === null ? "—" : `${fmtDuration(botAgeS)} old`;
}

// True while a /snapshot request is outstanding. Over an SSH tunnel a
// request can take several seconds — longer than POLL_MS — and the interval
// fired regardless, so three or four identical requests piled up in flight
// at once. That multiplies load on both the tunnel and a VPS that may
// already be CPU-starved, making the very slowness that caused the pile-up
// worse, and it is what makes the "live -> link down -> live" flapping
// self-sustaining. One in flight at a time; a bot switch still forces its
// own (see `force`), since that request supersedes whatever is pending.
let pollInFlight = false;

async function poll(force = false) {
  if (pollInFlight && !force) return;
  pollInFlight = true;
  try {
    await _poll();
  } finally {
    pollInFlight = false;
  }
}

async function _poll() {
  // Captured now, not read again after the await: the click handler fires a
  // poll() outside the regular interval so switching feels instant, which
  // means two requests can be in flight together (the new click's and the
  // previous tick's still-pending one for the old bot). Without pinning the
  // request to the bot it was actually sent for, whichever response lands
  // last wins the race — including the stale one for the bot the user just
  // switched away from, which would otherwise silently snap the selection
  // back.
  const requestedBot = currentBotKey;
  try {
    let url = `/snapshot?interval=${encodeURIComponent(currentInterval)}`;
    if (requestedBot) url += `&bot=${encodeURIComponent(requestedBot)}`;
    // Omitted means "the running session" — the server resolves it, so a
    // session that ends while the tab is open rolls over on its own rather
    // than pinning the panel to a run that is over.
    if (currentSession !== null) url += `&session=${encodeURIComponent(currentSession)}`;
    // Tell the server which history we already hold so it can answer
    // "unchanged" instead of resending thousands of identical candles.
    if (lastCandleSig) url += `&ksig=${encodeURIComponent(lastCandleSig)}`;
    const resp = await fetch(url, { cache: "no-store" });
    if (!resp.ok) throw new Error(`http ${resp.status}`);
    const data = await resp.json();

    // The user picked a different bot while this request was in flight — a
    // fresh poll for that bot is already on its way (the click handler
    // fires one immediately), so this now-stale response is dropped rather
    // than applied on top of it.
    if (requestedBot !== currentBotKey) return;

    renderBotList(data.bots, data.server_ts, data.bot);
    renderBasis(data.basis || []);
    renderIncidents(data.incidents || [], data.server_ts);
    // The server honours the requested bot when it still exists, so a
    // mismatch means either no choice had been made yet (first load lands on
    // the freshest bot) or the chosen bot vanished and was substituted.
    // Both cases want the same thing: adopt it and clear the old bot's chart.
    if (data.bot && data.bot !== currentBotKey) {
      currentBotKey = data.bot;
      resetChartState();
    }

    const venue = data.venue;
    if (venue) {
      currentSymbol = venue.symbol || "";
      // The tab title names whichever bot/venue is actually selected, not a
      // fixed exchange — this dashboard watches whatever the operator is
      // running today, not just Bitunix.
      document.title = venue.label ? `MM Dashboard — ${venue.label}` : "MM Dashboard";

      // Age is resolved *before* anything is drawn: what a bot asserts about
      // its position, PnL and book is only worth rendering while the bot is
      // still alive to assert it. A stopped bot's last words are not a
      // reading of the market, they are a fossil of the moment it died.
      if (data.server_ts && venue.source_ts) {
        botAgeAtPollS = Math.max(0, data.server_ts - venue.source_ts);
        botAgeMeasuredAt = Date.now();
      } else {
        botAgeAtPollS = null;
      }
      const dead = botAgeAtPollS !== null && botAgeAtPollS * 1000 > BOT_DEAD_MS;

      // The book is published by the bot, so it dies with it. Dropping it
      // here also clears the mid out of the chart label, which would
      // otherwise keep quoting a days-old price next to a live symbol name.
      lastOrderbook = dead ? null : venue.orderbook || null;
      lastQuotes = dead ? [] : venue.quotes || [];
      updatePriceDecimals(lastOrderbook);
      renderTimeframeBar(venue);
      updateChartLabel();
      // Candles are venue market data, not the bot's claim, so they stay
      // true after the bot stops and are still worth showing.
      renderCandles(venue.klines);

      // A session's realised performance outlives the bot that produced it,
      // so it is rendered either way — see renderDeadBot.
      renderSessionBar(venue.sessions, venue.session);
      renderStats(venue.stats, venue.session);
      renderCurve(venue.equity_curve);
      renderMarkout(venue.markouts);

      if (dead) {
        renderDeadBot(botAgeAtPollS);
      } else {
        addFillMarkers(venue.fills || [], venue.kline_interval_s);
        renderPriceLines(venue);
        renderPosition(venue);
        renderNav(venue.nav_curve);
        renderBook(bookVisible ? lastOrderbook : null, lastQuotes);
      }
    } else {
      // Nothing discovered at all. The link is healthy, so say so rather
      // than showing the last bot's numbers next to a green badge.
      currentBotKey = null;
      currentSymbol = "";
      lastOrderbook = null;
      lastQuotes = [];
      botAgeAtPollS = null;
      document.title = "MM Dashboard";
      resetChartState();
      renderPosition({ positions: [], account: null });
      renderStats(null);
      renderCurve(null);
      renderMarkout(null);
      renderNav(null);
      renderBook(null);
      updateChartLabel();
    }
    lastGoodPollTs = Date.now();
    everConnected = true;
  } catch (err) {
    console.warn("poll failed:", err.message);
  }
  renderStatus();
}

// Independent of the poll loop on purpose: this is what keeps the badge
// counting up during an outage, when poll() is throwing and would otherwise
// never run again.
setInterval(renderStatus, STATUS_TICK_MS);

poll();
setInterval(poll, POLL_MS);

// Safari (WebKit) throttles setInterval hard in a backgrounded tab — a
// click or a switch made just before tabbing away can sit unconfirmed for
// however long the tab was hidden, which reads as "stuck on the old bot"
// exactly like the tunnel-latency case above, just triggered by tab focus
// instead of network speed. Firing one poll the instant the tab becomes
// visible again means coming back never waits on a throttled timer to
// catch up on its own.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") poll(true);
});
