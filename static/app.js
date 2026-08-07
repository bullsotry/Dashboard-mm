/* Read-only dashboard frontend. Polls /snapshot; never sends anything back
 * except that GET. No websockets, no auth — this only runs behind an SSH
 * tunnel to 127.0.0.1 on the VPS. */

const POLL_MS = 1500;
// Two independent failure modes, two independent clocks:
//   link  — we cannot reach our own server (tunnel dropped, uvicorn died).
//   bot   — the server answers fine, but the bot stopped writing its state.
// The second is the dangerous one: everything still renders, just frozen.
const LINK_STALE_MS = 6000;
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
    updateChartLabel();
    poll(); // don't wait for the next tick — the click should feel instant
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
  const sig = `${klines.length}|${klines[0].time}|${last.time}|${last.close}|${last.high}|${last.low}`;
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
  const meta = { len: klines.length, first: klines[0].time, last: last.time };
  const sameTail = lastBarsMeta && lastBarsMeta.first === meta.first;
  const inPlace = sameTail && meta.len === lastBarsMeta.len && meta.last === lastBarsMeta.last;
  const appended = sameTail && meta.len === lastBarsMeta.len + 1 && meta.last > lastBarsMeta.last;

  if (inPlace) {
    candleSeries.update(bar(last));
  } else if (appended) {
    // Finalise the bar that just closed before adding the new one; update()
    // requires non-decreasing times, so the older one has to go first.
    candleSeries.update(bar(klines[klines.length - 2]));
    candleSeries.update(bar(last));
  } else {
    // History changed shape — deeper pagination arrived, or the bot or
    // interval changed. A full reset is correct here.
    candleSeries.setData(klines.map(bar));
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
        <div class="row"><span class="label">margin used</span><span class="val">${fmt(a.margin_used, 2)}</span></div>
        <div class="row"><span class="label">cross uPnL</span><span class="val ${pnlCls}">${fmt(a.unrealised_pnl, 2)}</span></div>
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

function renderBasis(pairs) {
  const body = document.getElementById("basis-body");
  if (!pairs || pairs.length === 0) {
    body.innerHTML = `<div class="empty-note">need 2 legs on different venues</div>`;
    return;
  }
  body.innerHTML = pairs
    .map((p) => {
      const cls = p.bps >= 0 ? "pnl-pos" : "pnl-neg";
      return `<div class="row basis-row">
        <span class="label">${p.label}</span>
        <span class="val ${cls}">${sparklineSvg(p.history)}${fmt(p.bps, 1)} bps</span>
      </div>`;
    })
    .join("");
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
let lastBotsSig = "";

function resetChartState() {
  symbolDecimals = null; // a new instrument has a different tick
  seenFillTimes = new Set();
  fillBuckets = new Map();
  fillMarkers.setMarkers([]);
  candleSeries.setData([]);
  lastCandleSig = "";
  lastLineSig = "";
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
function botSummaryLine(bot, dead) {
  if (dead) return `<span class="empty-note" style="font-size:10.5px;">stopped</span>`;

  const positions = bot.positions || [];
  if (positions.length === 0) {
    const net =
      bot.pnl_unreliable || bot.realised_net === null || bot.realised_net === undefined
        ? ""
        : `<span class="val ${bot.realised_net >= 0 ? "pnl-pos" : "pnl-neg"}">${fmt(bot.realised_net, 4)}</span>`;
    return `<span class="label">flat</span>${net}`;
  }

  let netQty = 0;
  let upnl = bot.account_upnl ?? 0;
  let hasPosUpnl = false;
  for (const p of positions) {
    netQty += (p.side === "LONG" ? 1 : -1) * (p.qty_base || 0);
    if (p.unrealised_pnl !== null && p.unrealised_pnl !== undefined) {
      upnl += p.unrealised_pnl;
      hasPosUpnl = true;
    }
  }
  const side = netQty > 0 ? "long" : netQty < 0 ? "short" : "flat";
  const showUpnl = hasPosUpnl || bot.account_upnl !== null;
  const pnlCls = upnl >= 0 ? "pnl-pos" : "pnl-neg";
  return (
    `<span class="pos-side-label ${side}">${side}</span>` +
    `<span class="val">${fmt(Math.abs(netQty), 3)}</span>` +
    (showUpnl ? `<span class="val ${pnlCls}">${fmt(upnl, 4)}</span>` : "")
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
        `|${JSON.stringify(b.positions || [])}|${b.realised_net}|${b.account_upnl}`
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

  resetChartState();
  lastBotsSig = "";
  poll();
});

function fmtDuration(s) {
  if (!s || s <= 0) return "—";
  if (s < 90) return `${Math.round(s)}s`;
  if (s < 5400) return `${Math.round(s / 60)}m`;
  if (s < 172800) return `${(s / 3600).toFixed(1)}h`;
  return `${(s / 86400).toFixed(1)}d`;
}

function renderStats(stats) {
  const body = document.getElementById("stats-body");
  const windowEl = document.getElementById("stats-window");

  if (!stats || stats.n_fills === 0) {
    windowEl.textContent = "—";
    body.innerHTML = `<div class="empty-note">no fills in window</div>`;
    return;
  }

  // The header states the window these numbers actually cover. Without it
  // every figure below invites the reader to assume "since I started
  // watching", which is never what it means.
  windowEl.textContent = `last ${stats.n_fills} fills · ${fmtDuration(stats.span_s)}`;

  const capture =
    stats.capture_bps === null || stats.capture_bps === undefined
      ? "—"
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

  if (stats.pnl_unreliable) {
    // A PnL that cannot be defended is not shown as a number. Printing it
    // greyed out would still leave a figure on screen to be read and
    // believed, which is exactly how the wrong one got trusted.
    body.innerHTML =
      measured +
      `<div class="stat-net pnl-blocked">
         <div class="pnl-blocked-title">realised pnl unavailable</div>
         <div class="pnl-blocked-why">${stats.pnl_unreliable}</div>
       </div>`;
    return;
  }

  const netCls = stats.realised_net >= 0 ? "pnl-pos" : "pnl-neg";
  body.innerHTML =
    measured +
    `
    <div class="row"><span class="label">inventory</span><span class="val">${fmt(stats.inventory_base, 3)}</span></div>
    <div class="row"><span class="label">realised gross</span><span class="val">${fmt(stats.realised_gross, 4)}</span></div>
    <div class="row stat-net"><span class="label">realised net</span><span class="val ${netCls}">${fmt(stats.realised_net, 4)}</span></div>
  `;
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
    dash("available") + dash("margin used") + dash("cross uPnL") +
    `</div>` + stopped;

  document.getElementById("stats-window").textContent = "—";
  document.getElementById("stats-body").innerHTML =
    dash("fills") + dash("rate") + dash("volume") +
    dash("fees paid") + dash("capture") +
    `<div class="row stat-net"><span class="label">realised net</span><span class="val">—</span></div>` +
    stopped;

  renderBook(null);
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
  // Compact on purpose — the rail is support, the chart is the content.
  const depth = 7;
  const asks = ob.asks.slice(0, depth).reverse();
  const bids = ob.bids.slice(0, depth);
  const maxSize = Math.max(1e-9, ...asks.map((l) => l.size), ...bids.map((l) => l.size));
  const bookRow = (l, side) => {
    const pct = Math.min(100, (l.size / maxSize) * 100);
    return `<div class="book-row ${side}"><div class="depth-bar" style="width:${pct}%"></div><span>${fmtPrice(l.price)}</span><span>${fmt(l.size, 3)}</span></div>`;
  };
  asksEl.innerHTML = asks.map((l) => bookRow(l, "ask")).join("");
  bidsEl.innerHTML = bids.map((l) => bookRow(l, "bid")).join("");
  const spreadBps = ob.mid > 0 ? ((ob.best_ask - ob.best_bid) / ob.mid) * 10000 : 0;
  spreadEl.textContent = `spread ${fmtPrice(ob.best_ask - ob.best_bid)} (${fmt(spreadBps, 1)} bps)`;
}

const bookPanel = document.getElementById("book-panel");
const bookToggle = document.getElementById("book-toggle");
// Visible by default: it sits high in the rail now and is meant to be
// read at a glance, not opened on demand.
let bookVisible = true;
let lastOrderbook = null;
bookToggle.addEventListener("click", () => {
  bookVisible = !bookVisible;
  bookPanel.classList.toggle("visible", bookVisible);
  bookToggle.textContent = bookVisible ? "hide" : "show";
  renderBook(bookVisible ? lastOrderbook : null); // don't wait for next poll tick
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
}

async function poll() {
  try {
    let url = `/snapshot?interval=${encodeURIComponent(currentInterval)}`;
    if (currentBotKey) url += `&bot=${encodeURIComponent(currentBotKey)}`;
    // Tell the server which history we already hold so it can answer
    // "unchanged" instead of resending thousands of identical candles.
    if (lastCandleSig) url += `&ksig=${encodeURIComponent(lastCandleSig)}`;
    const resp = await fetch(url, { cache: "no-store" });
    if (!resp.ok) throw new Error(`http ${resp.status}`);
    const data = await resp.json();

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
      updatePriceDecimals(lastOrderbook);
      renderTimeframeBar(venue);
      updateChartLabel();
      // Candles are venue market data, not the bot's claim, so they stay
      // true after the bot stops and are still worth showing.
      renderCandles(venue.klines);

      if (dead) {
        renderDeadBot(botAgeAtPollS);
      } else {
        addFillMarkers(venue.fills || [], venue.kline_interval_s);
        renderPriceLines(venue);
        renderPosition(venue);
        renderStats(venue.stats);
        renderBook(bookVisible ? lastOrderbook : null);
      }
    } else {
      // Nothing discovered at all. The link is healthy, so say so rather
      // than showing the last bot's numbers next to a green badge.
      currentBotKey = null;
      currentSymbol = "";
      lastOrderbook = null;
      botAgeAtPollS = null;
      document.title = "MM Dashboard";
      resetChartState();
      renderPosition({ positions: [], account: null });
      renderStats(null);
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
