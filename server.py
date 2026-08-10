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
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import config
from adapters.basis import compute_basis_pairs
from adapters.bitunix_klines import DEFAULT_INTERVAL
from adapters.bot_files import BotStateAdapter
from adapters.discovery import DiscoveredBot, discover

app = FastAPI()

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
            _last_class[key] = cls
        # A bot dropped by discovery (its files were deleted) has nothing
        # left to classify; drop its bookkeeping so it doesn't linger forever.
        for key in set(_last_class) - set(found):
            _last_class.pop(key, None)
            _state_history.pop(key, None)


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


threading.Thread(target=_background_state_loop, daemon=True).start()


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

        found = {b.key: b for b in discover(config.VIZ_GLOBS, config.FILLS_GLOBS)}

        for key, bot in found.items():
            existing = _bots.get(key)
            # Rebuild the reader if the bot moved to a different file, so a
            # relocated or restarted-elsewhere bot doesn't keep serving the
            # old path's contents.
            if existing is None or existing.viz_path != bot.viz_path or existing.fills_path != bot.fills_path:
                _state_adapters[key] = BotStateAdapter(
                    venue_name=bot.exchange,
                    symbol=bot.symbol,
                    viz_path=bot.viz_path,
                    fills_path=bot.fills_path,
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


def _compute_basis(found: dict[str, DiscoveredBot]) -> list[dict]:
    """Cross-venue mid basis for every pair of legs sharing an asset.

    Reads each bot's own orderbook (already-cached local file, no network
    call), so this costs one extra dict read per discovered bot per poll —
    negligible next to the kline/account fetches already happening. History
    is appended here rather than in a background loop: a pair with no active
    poller (nobody has this dashboard open) correctly keeps no history.
    """
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
            }
        )

    out = []
    for pair in compute_basis_pairs(legs):
        hist_key = f"{pair['a']}|{pair['b']}"
        hist = _basis_history.setdefault(hist_key, deque(maxlen=config.BASIS_HISTORY_LEN))
        hist.append(pair["bps"])
        out.append({**pair, "history": list(hist)})
    return out


def _bot_summary(key: str, bot_info: DiscoveredBot) -> dict:
    """Enough of one bot's state to render it as a row in the portfolio
    table, without the caller having to select it first. Cheap: positions and
    stats are local-file reads already cached incrementally by the adapter,
    and the account adapter caches its own REST poll independently — calling
    it here for every discovered bot on every snapshot does not add REST
    calls beyond what that adapter's own poll interval already allows."""
    adapter = _state_adapters[key]
    stats = adapter.get_stats()
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
        "account_upnl": account["unrealised_pnl"] if account else None,
    }


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
) -> dict:
    """`ksig` is the candle signature the caller already holds. Deep history
    made the candles ~82% of every response while changing at most once per
    kline poll, so an unchanged history is answered with its signature alone
    instead of thousands of identical rows."""
    found = _refresh()
    if not found:
        return {"server_ts": time.time(), "bot": None, "bots": [], "venue": None, "basis": [], "incidents": []}

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

    klines = (
        kline_adapter.get_klines(interval, since_ts=state.get_first_fill_ts())
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
        "stats": state.get_stats(),
        # Same FIFO replay as `stats`, kept as a per-fill series instead of
        # collapsed to a total — feeds the Curve (cumulative realised PnL),
        # Delta (running inventory) and Volume (cumulative traded notional)
        # panels.
        "equity_curve": state.get_equity_curve(),
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
