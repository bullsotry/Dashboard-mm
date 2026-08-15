"""Read-only monitoring dashboard. Never places orders, never imports the
bot's code, never writes to anything a bot reads.

Two routes: /bots lists what has been discovered, /snapshot returns the live
state of one of them. Everything else is a static file.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import config
from adapters.basis import compute_basis_pairs, split_stale
from adapters.bitunix_klines import DEFAULT_INTERVAL
from adapters.bot_files import BotStateAdapter
from adapters.discovery import DiscoveredBot, discover

app = FastAPI()


# StaticFiles answers with an ETag but no Cache-Control, which leaves each
# browser's own heuristics to decide how long "fresh" means — Safari in
# particular has been seen serving a stale disk-cached JS bundle across a
# plain reload, so a code fix landing on the server can silently never reach
# an already-open tab no matter how many times it's redeployed. `no-cache`
# doesn't disable caching, it just forces a conditional GET (If-None-Match)
# on every load, so a real reload always finds out whether the file changed
# instead of trusting a cached copy's guess.
@app.middleware("http")
async def _no_cache_static(request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


# Compression. /snapshot is a couple of hundred KB of JSON resent every 1.5s
# poll, over an SSH tunnel — the single biggest thing crossing that link.
# Measured on a fixture snapshot: 195.6KB -> 11.3KB. Treat that 17x as an
# upper bound, not a forecast: the fixture ledger cycles through ten prices,
# which deflate loves. The same payload shape with randomised values
# compresses ~5x, so ~5x is the number to expect on a real bot.
#
# compresslevel=5, not the library default of 9: measured on that payload,
# 9 gives 10.5KB in 7.22ms against 5's 11.3KB in 4.99ms — 7% smaller for 45%
# more CPU, on a VPS that already shares its cores with the bots it watches.
#
# Registered after the no-cache middleware so it wraps it. Verified that
# static revalidation still works through it (conditional GET returns a real
# 304 with an empty body, a stale ETag returns 200 gzipped) — that 304 path
# is what keeps a redeployed app.js from being served stale by Safari.
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)

_STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

# Adapters are cached per bot key and reused across polls: each holds an
# incremental read offset into the fill ledger and a candle cache, both of
# which would be thrown away by rebuilding them every request.
_lock = threading.Lock()
_bots: dict[str, DiscoveredBot] = {}
_state_adapters: dict[str, BotStateAdapter] = {}
_kline_adapters: dict[str, object] = {}
_account_adapters: dict[str, object] = {}
_last_scan_ts = 0.0

# What the kline warm loop should keep hot: bot key -> (interval, since_ts,
# last_requested_ts). Written by /snapshot, read by the loop. Only intervals
# a browser has actually asked for within KLINE_WARM_TTL_S are refreshed —
# warming all 8 granularities of every bot would multiply this dashboard's
# REST footprint for candles nobody is looking at.
_kline_warm: dict[str, tuple[str, float | None, float]] = {}

# Basis history is keyed by "leg_a|leg_b" (stable per venue pair) and kept
# across polls independently of which bot is selected in the chart — the
# basis panel shows every discovered pair, not just the active one.
_basis_history: dict[str, deque] = {}

# Account equity, one sample per bot per poll — the NAV curve, unlike the
# realised-PnL Curve, has no source-of-truth history to replay from the fill
# ledger (equity depends on venue-side margin/collateral too, not just this
# bot's own fills), so a sampled series while the dashboard is open is the
# only history there is. Same "no active poller, no history" property as
# _basis_history above.
_nav_history: dict[str, deque] = {}

# Per-bot live/warn/dead transition log, and the classification each bot was
# last seen in (so a transition is only recorded on an actual change, not on
# every background tick). Written only by _record_transitions, running on
# its own thread below — independent of whether a browser is polling, so a
# halt that happens unattended still lands in the timeline.
_state_history: dict[str, deque] = {}
_last_class: dict[str, str | None] = {}

# When this dashboard watched each bot come back to life. The tracker's
# session log is authoritative for *finished* sessions, but the running one
# has no row yet, and its true start is the process start — which the ledger
# can only lower-bound by its first fill. This is the better answer when we
# happen to have been watching, and it survives a browser closing (the
# liveness thread runs regardless); it does not survive this process
# restarting, which is exactly why it is a preference and not the source.
_observed_session_start: dict[str, float] = {}


def _classify(age_s: float | None) -> str | None:
    if age_s is None:
        return None
    if age_s > config.BOT_DEAD_S:
        return "dead"
    if age_s > config.BOT_STALE_S:
        return "warn"
    return "live"


def _record_transitions(found: dict[str, DiscoveredBot]) -> None:
    now = time.time()
    with _lock:
        for key in found:
            adapter = _state_adapters.get(key)
            src_ts = adapter.get_source_ts() if adapter else None
            cls = _classify(now - src_ts if src_ts else None)
            prev = _last_class.get(key)
            # prev is None only on the very first observation of this bot —
            # that is discovery, not an incident, so it is not logged.
            if prev is not None and cls != prev:
                hist = _state_history.setdefault(key, deque(maxlen=config.STATE_HISTORY_MAXLEN))
                hist.append({"ts": now, "from": prev, "to": cls})
                # A bot coming back from dead is a new session starting, and
                # this is the closest thing to the process start that can be
                # observed from outside it. `src_ts` (the bot's own heartbeat)
                # rather than `now`, so the start doesn't drift by however
                # long this thread's tick happened to be.
                if prev == "dead" and cls == "live":
                    _observed_session_start[key] = src_ts or now
            _last_class[key] = cls
        # A bot dropped by discovery (its files were deleted) has nothing
        # left to classify; drop its bookkeeping so it doesn't linger forever.
        for key in set(_last_class) - set(found):
            _last_class.pop(key, None)
            _state_history.pop(key, None)
            _observed_session_start.pop(key, None)


def _incidents_feed(found: dict[str, DiscoveredBot]) -> list[dict]:
    events = []
    for key, hist in _state_history.items():
        if key not in found:
            continue
        label = found[key].label
        for ev in hist:
            events.append({**ev, "bot": key, "label": label})
    events.sort(key=lambda e: e["ts"], reverse=True)
    return events[: config.INCIDENTS_LIMIT]


def _background_state_loop() -> None:
    """Ticks on its own clock so incidents are caught whether or not a
    browser tab happens to be open polling /snapshot at that moment."""
    while True:
        try:
            _record_transitions(_refresh())
        except Exception:
            # A monitoring loop must never die from a transient error (a
            # glob race, a half-written file) — it has nothing to report to
            # and no caller to raise to; it just tries again next tick.
            pass
        time.sleep(config.STATE_POLL_S)


def _account_warm_loop() -> None:
    """Keeps every account adapter's cache hot off the request path.

    Each account adapter refreshes itself over signed REST on its own
    interval, but it did so *inside* whichever /snapshot call happened to
    arrive after its cache expired — and /snapshot asks every discovered bot
    for its account, so one unlucky request paid three or four sequential
    round trips and took over a second while the rest took 0.1s. That is the
    periodic stall you can watch in the UI, and on a slow link it is enough
    to trip the frontend's own "link down" threshold.

    Ticking faster than the adapters' TTL means the expiry is essentially
    always reached here first, so requests find a warm cache and return
    without touching the network. Nothing is computed or stored here: this
    only pre-pays the same fetch the request would otherwise have made, so
    the numbers served are identical either way.
    """
    interval = max(0.5, config.ACCOUNT_POLL_INTERVAL_S / 2)
    while True:
        try:
            for adapter in list(_account_adapters.values()):
                if adapter is not None:
                    adapter.get_account()  # returns None on failure, never raises
        except Exception:
            pass  # same reasoning as _background_state_loop
        time.sleep(interval)


def _kline_warm_loop() -> None:
    """Keeps the candle cache hot off the request path.

    Same defect the account warm loop was written for, left standing on the
    klines: /snapshot called get_klines() inline, and a cold or expired
    interval fetches up to MAX_PAGES_PER_CALL pages at a 5s timeout — a
    potential ~40s stall on a request the operator is watching, and roughly
    one round trip in every 13 polls in steady state (a 20s adapter TTL
    against a 1.5s poll).

    Now the request path calls get_klines(serve_only=True), which never
    fetches when it has bars, and this loop does the fetching. Ticking at
    half the adapter's own poll interval means the expiry is essentially
    always reached here first, so requests find a warm cache.

    Nothing is computed or stored here — it pre-pays exactly the fetch the
    request would have made, so the candles served are identical either way.
    """
    interval = max(0.5, config.KLINE_POLL_INTERVAL_S / 2)
    while True:
        try:
            now = time.time()
            with _lock:
                targets = {
                    key: (iv, since_ts)
                    for key, (iv, since_ts, asked_ts) in _kline_warm.items()
                    if now - asked_ts <= config.KLINE_WARM_TTL_S
                }
                # Drop what nobody has asked for in a while (tab closed, or
                # the operator switched timeframe) so it stops costing REST.
                for key in set(_kline_warm) - set(targets):
                    _kline_warm.pop(key, None)
            for key, (iv, since_ts) in targets.items():
                adapter = _kline_adapters.get(key)
                if adapter is not None:
                    # Full behaviour, backfill included: this thread is the
                    # one that is allowed to wait on the venue.
                    adapter.get_klines(iv, since_ts=since_ts)
        except Exception:
            pass  # same reasoning as _background_state_loop
        time.sleep(interval)


threading.Thread(target=_background_state_loop, daemon=True).start()
threading.Thread(target=_account_warm_loop, daemon=True).start()
threading.Thread(target=_kline_warm_loop, daemon=True).start()


def _refresh(force: bool = False) -> dict[str, DiscoveredBot]:
    """Rescan for bots, at most every DISCOVERY_INTERVAL_S.

    This is what makes the dashboard worth leaving running: a bot started
    an hour after the dashboard appears on its own, and one that is deleted
    disappears. A bot that merely *stopped* keeps its entry — its state file
    still exists, and its timestamp is what marks it dead. Dropping it would
    be indistinguishable from it never having run.
    """
    global _last_scan_ts
    now = time.time()
    with _lock:
        if not force and now - _last_scan_ts < config.DISCOVERY_INTERVAL_S:
            return dict(_bots)
        _last_scan_ts = now

        found = {
            b.key: b
            for b in discover(
                config.VIZ_GLOBS, config.FILLS_GLOBS, exclude=config.DISCOVERY_EXCLUDE
            )
        }

        for key, bot in found.items():
            existing = _bots.get(key)
            # Rebuild the reader if the bot moved to a different file, so a
            # relocated or restarted-elsewhere bot doesn't keep serving the
            # old path's contents.
            if existing is None or existing.viz_path != bot.viz_path or existing.fills_paths != bot.fills_paths:
                _state_adapters[key] = BotStateAdapter(
                    venue_name=bot.exchange,
                    symbol=bot.symbol,
                    viz_path=bot.viz_path,
                    fills_paths=bot.fills_paths,
                    fills_maxlen=config.FILLS_MAXLEN,
                )
            if key not in _kline_adapters:
                _kline_adapters[key] = config.build_kline_adapter(bot.exchange, bot.symbol)
            if key not in _account_adapters:
                _account_adapters[key] = config.build_account_adapter(bot.exchange, bot.symbol)

        for key in set(_bots) - set(found):
            _state_adapters.pop(key, None)
            _kline_adapters.pop(key, None)
            _account_adapters.pop(key, None)
            _nav_history.pop(key, None)
            _kline_warm.pop(key, None)

        _bots.clear()
        _bots.update(found)
        return dict(_bots)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


@app.get("/bots")
def bots() -> dict:
    """The bot list, with just enough per bot for the frontend to render a
    selector and show which ones are alive without fetching every snapshot."""
    found = _refresh()
    out = []
    for key in sorted(found):
        adapter = _state_adapters.get(key)
        out.append(
            {
                "key": key,
                "exchange": found[key].exchange,
                "symbol": found[key].symbol,
                "label": found[key].label,
                "source_ts": adapter.get_source_ts() if adapter else None,
                "has_chart": _kline_adapters.get(key) is not None,
            }
        )
    return {"server_ts": time.time(), "bots": out}


def _compute_basis(found: dict[str, DiscoveredBot]) -> dict:
    """Cross-venue mid basis for every pair of legs sharing an asset.

    Reads each bot's own orderbook (already-cached local file, no network
    call), so this costs one extra dict read per discovered bot per poll —
    negligible next to the kline/account fetches already happening. History
    is appended here rather than in a background loop: a pair with no active
    poller (nobody has this dashboard open) correctly keeps no history.

    Legs whose mid is too old to compare are dropped *before* pairing and
    returned separately, so the panel can name what it refused to price
    instead of silently showing fewer rows — see adapters/basis.py.
    """
    now = time.time()
    legs = []
    for key, bot in found.items():
        adapter = _state_adapters.get(key)
        ob = adapter.get_orderbook() if adapter else None
        legs.append(
            {
                "key": key,
                "exchange": bot.exchange,
                "symbol": bot.symbol,
                "mid": ob["mid"] if ob else 0.0,
                "ts": ob["ts"] if ob else 0.0,
            }
        )

    fresh, stale = split_stale(legs, now=now, max_age_s=config.BASIS_MAX_AGE_S)

    out = []
    for pair in compute_basis_pairs(fresh, now=now):
        hist_key = f"{pair['a']}|{pair['b']}"
        hist = _basis_history.setdefault(hist_key, deque(maxlen=config.BASIS_HISTORY_LEN))
        hist.append(pair["bps"])
        out.append({**pair, "history": list(hist)})

    # A pair that goes stale must not keep serving the history it had while
    # it was live: the sparkline would carry on showing a shape for a number
    # that no longer exists.
    live_keys = {f"{p['a']}|{p['b']}" for p in out}
    for hist_key in set(_basis_history) - live_keys:
        _basis_history.pop(hist_key, None)

    return {"pairs": out, "stale": stale}


def _resolve_session(key: str, adapter: BotStateAdapter, requested: str | None):
    """The session to report on, and the full list to offer.

    Defaults to the running one — the operator's "how is tonight going" —
    and falls back to the most recent finished session when the bot is
    stopped, so a dashboard opened the morning after still shows the night
    rather than an empty panel.
    """
    sessions = adapter.get_sessions(_observed_session_start.get(key))
    if not sessions:
        return None, []
    selected = None
    if requested is not None:
        selected = next((s for s in sessions if str(s.index) == requested), None)
    if selected is None:
        selected = sessions[-1]
    return selected, sessions


def _session_payload(s, server_ts: float) -> dict:
    return {
        "index": s.index,
        "start_ts": s.start_ts,
        "end_ts": s.end_ts,
        "is_current": s.is_current,
        "clean_exit": s.clean_exit,
        # How the start was established, so the panel can say whether it is
        # the process start or merely the first fill seen since the previous
        # session — the two differ by however long the bot ran before it
        # traded.
        "start_source": s.start_source,
        "elapsed_s": (s.end_ts or server_ts) - s.start_ts,
    }


def _bot_summary(key: str, bot_info: DiscoveredBot) -> dict:
    """Enough of one bot's state to render it as a row in the portfolio
    table, without the caller having to select it first. Cheap: positions and
    stats are local-file reads already cached incrementally by the adapter,
    and the account adapter caches its own REST poll independently — calling
    it here for every discovered bot on every snapshot does not add REST
    calls beyond what that adapter's own poll interval already allows."""
    adapter = _state_adapters[key]
    # Every portfolio row is scoped to that bot's own current session, so the
    # column means the same thing as the Performance panel instead of being
    # a different window with the same label.
    session, _ = _resolve_session(key, adapter, None)
    stats = adapter.get_stats(
        since_ts=session.start_ts if session else None,
        until_ts=session.end_ts if session else None,
    )
    account_adapter = _account_adapters.get(key)
    account = account_adapter.get_account() if account_adapter else None
    if account is not None:
        hist = _nav_history.setdefault(key, deque(maxlen=config.NAV_HISTORY_LEN))
        hist.append({"ts": time.time(), "equity": account["equity"]})
    return {
        "key": key,
        "label": bot_info.label,
        "exchange": bot_info.exchange,
        "symbol": bot_info.symbol,
        "source_ts": adapter.get_source_ts(),
        "positions": adapter.get_positions(),
        # None when the ledger can't defend a figure — same refusal as the
        # selected bot's own stats panel, so a portfolio row never states a
        # realised PnL the fills window doesn't support.
        "realised_net": None if stats.get("pnl_unreliable") else stats["realised_net"],
        "pnl_unreliable": stats.get("pnl_unreliable"),
        # Survives what the realised figure can't (hedge mode above all), so
        # a row is not left blank on the leg doing most of the volume.
        "cash_pnl": stats.get("cash_pnl"),
        "session_index": session.index if session else None,
        "account_upnl": account["unrealised_pnl"] if account else None,
    }


def _decimate_curve(curve: list[dict], max_points: int) -> list[dict]:
    """Thin a per-fill series down to something a 200px mini-chart can
    actually show, without flattening what it is there to reveal.

    A busy leg produces thousands of points per session — the Coinbase one
    reached 4447, which is 976KB of the 1296KB response, resent every 1.5s
    poll and enough to make the UI stall for seconds at a time behind an SSH
    tunnel. No chart that size can render 4447 distinct points anyway.

    Plain every-Nth sampling would drop the extremes, which on a PnL curve
    means quietly erasing the drawdown it exists to show. So each bucket
    contributes its minimum *and* its maximum point, in time order: the
    envelope survives, and the last point is always kept so the live value
    and `max_drawdown` (computed over the full series upstream, not from
    these points) stay exact.
    """
    if len(curve) <= max_points:
        return curve
    buckets = max(1, max_points // 2)
    size = len(curve) / buckets
    out: list[dict] = []
    for i in range(buckets):
        chunk = curve[int(i * size) : int((i + 1) * size)]
        if not chunk:
            continue
        lo = min(chunk, key=lambda p: p["realised_net"])
        hi = max(chunk, key=lambda p: p["realised_net"])
        for p in sorted({id(lo): lo, id(hi): hi}.values(), key=lambda p: p["ts"]):
            out.append(p)
    if out and out[-1] is not curve[-1]:
        out.append(curve[-1])
    return out


def _klines_sig(candles: list) -> str:
    """Cheap identity for a candle list. Only the newest bar mutates in place
    (the one still forming), so its OHLC plus volume plus the list bounds is
    enough to tell "same history" from "something moved". Volume is included
    because a trade can land inside the forming bar without moving OHLC (same
    price as the last trade) — without it, the sig would miss that tick."""
    if not candles:
        return "0"
    last = candles[-1]
    return (
        f'{len(candles)}|{candles[0]["time"]}|{last["time"]}|'
        f'{last["close"]}|{last["high"]}|{last["low"]}|{last.get("volume")}'
    )


@app.get("/snapshot")
def snapshot(
    bot: str | None = None,
    interval: str = DEFAULT_INTERVAL,
    ksig: str | None = None,
    session: str | None = None,
) -> dict:
    """`ksig` is the candle signature the caller already holds. Deep history
    made the candles ~82% of every response while changing at most once per
    kline poll, so an unchanged history is answered with its signature alone
    instead of thousands of identical rows."""
    found = _refresh()
    now_ts = time.time()
    if not found:
        return {
            "server_ts": time.time(),
            "bot": None,
            "bots": [],
            "venue": None,
            "basis": {"pairs": [], "stale": []},
            "incidents": [],
        }

    # Spans every discovered bot, not just the selected one, so the basis
    # panel keeps reading even while the chart is pointed at a single leg.
    basis = _compute_basis(found)
    incidents = _incidents_feed(found)

    # An unknown or omitted bot falls back to the freshest one, so a first
    # load with no selection lands on whatever is actually running.
    if bot not in found:
        bot = max(
            found,
            key=lambda k: (_state_adapters[k].get_source_ts() or 0.0),
        )

    state = _state_adapters[bot]
    kline_adapter = _kline_adapters.get(bot)
    account_adapter = _account_adapters.get(bot)

    # Validated against *this* venue's granularities, before it reaches an
    # external API call. Venues don't offer the same set — Bitunix has 3m
    # and 10m, Coinbase has 5m and 1d — so a global whitelist would either
    # reject an interval the venue supports or forward one it doesn't. When
    # the client asks for an interval this venue lacks (it just switched
    # bots), fall back to the venue's own default rather than erroring.
    supported = list(getattr(kline_adapter, "supported_intervals", [])) if kline_adapter else []
    if kline_adapter and interval not in supported:
        interval = kline_adapter.default_interval

    selected_session, all_sessions = _resolve_session(bot, state, session)
    session_start = selected_session.start_ts if selected_session else None
    # None for the running session, so it keeps extending; a finished one is
    # closed at its recorded end, otherwise every past session would silently
    # include everything traded since.
    session_end = selected_session.end_ts if selected_session else None

    # The chart follows the session too: an operator looking at tonight's
    # performance wants tonight's candles, not every bar back to the oldest
    # fill still in the ledger.
    kline_since = session_start or state.get_first_fill_ts()
    # Tell the warm loop what this tab is looking at, then read the cache it
    # keeps hot. `serve_only` is what keeps a venue round trip off this
    # thread — see _kline_warm_loop and the adapters' get_klines docstring.
    if kline_adapter is not None:
        with _lock:
            _kline_warm[bot] = (interval, kline_since, now_ts)
    klines = (
        kline_adapter.get_klines(interval, since_ts=kline_since, serve_only=True)
        if kline_adapter
        else []
    )
    klines_sig = _klines_sig(klines)
    # None means "unchanged, keep what you have" — distinct from [], which
    # means this bot genuinely has no candles.
    if ksig is not None and ksig == klines_sig:
        klines = None

    venue = {
        "key": bot,
        "exchange": found[bot].exchange,
        "symbol": found[bot].symbol,
        "label": found[bot].label,
        "orderbook": state.get_orderbook(),
        "positions": state.get_positions(),
        "fills": state.get_recent_fills(),
        "quotes": state.get_quotes(),
        "stats": state.get_stats(since_ts=session_start, until_ts=session_end),
        # Which run these figures cover, and every run available to switch
        # to. A performance number whose window isn't stated isn't a
        # measurement — this is that window, on screen.
        "session": _session_payload(selected_session, now_ts) if selected_session else None,
        # Where the mid went after each fill of this session — the measure
        # that says whether the spread captured at t=0 survives. See
        # adapters/markouts.py.
        "markouts": state.get_markouts(since_ts=session_start, until_ts=session_end),
        "sessions": [_session_payload(s, now_ts) for s in all_sessions],
        # Same FIFO replay as `stats`, kept as a per-fill series instead of
        # collapsed to a total — feeds the Curve (cumulative realised PnL),
        # Delta (running inventory) and Volume (cumulative traded notional)
        # panels.
        "equity_curve": _decimate_curve(
            state.get_equity_curve(since_ts=session_start, until_ts=session_end),
            config.CURVE_MAX_POINTS,
        ),
        # The bot's own heartbeat. Distinct from server_ts below: this server
        # answers happily while the bot it watches is dead, and the frontend
        # must be able to tell those two apart.
        "source_ts": state.get_source_ts(),
        "account": account_adapter.get_account() if account_adapter else None,
        "klines": klines,
        "klines_sig": klines_sig,
        "kline_interval": interval,
        "kline_interval_s": kline_adapter.interval_seconds(interval) if kline_adapter else 60,
        "supported_intervals": supported,
    }

    # Every bot's summary is (re)built before this line, so the selected
    # bot's own account sample has already landed in _nav_history this poll.
    bot_list = [_bot_summary(k, found[k]) for k in sorted(found)]
    venue["nav_curve"] = list(_nav_history.get(bot, []))

    # Server clock, so the frontend measures bot staleness against the same
    # clock that produced source_ts instead of against the browser's, which
    # can be minutes off and would make a healthy bot look dead.
    return {
        "server_ts": time.time(),
        "bot": bot,
        "bots": bot_list,
        "venue": venue,
        "basis": basis,
        "incidents": incidents,
    }
