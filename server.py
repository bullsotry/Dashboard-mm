"""Read-only monitoring dashboard. Never places orders, never imports the
bot's code, never writes to anything a bot reads.

Two routes: /bots lists what has been discovered, /snapshot returns the live
state of one of them. Everything else is a static file.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import config
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
                _account_adapters[key] = config.build_account_adapter(bot.exchange)

        for key in set(_bots) - set(found):
            _state_adapters.pop(key, None)
            _kline_adapters.pop(key, None)
            _account_adapters.pop(key, None)

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


def _klines_sig(candles: list) -> str:
    """Cheap identity for a candle list. Only the newest bar mutates in place
    (the one still forming), so its OHLC plus the list bounds is enough to
    tell "same history" from "something moved"."""
    if not candles:
        return "0"
    last = candles[-1]
    return f'{len(candles)}|{candles[0]["time"]}|{last["time"]}|{last["close"]}|{last["high"]}|{last["low"]}'


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
        return {"server_ts": time.time(), "bot": None, "bots": [], "venue": None}

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

    bot_list = [
        {
            "key": k,
            "label": found[k].label,
            "source_ts": _state_adapters[k].get_source_ts(),
        }
        for k in sorted(found)
    ]

    # Server clock, so the frontend measures bot staleness against the same
    # clock that produced source_ts instead of against the browser's, which
    # can be minutes off and would make a healthy bot look dead.
    return {"server_ts": time.time(), "bot": bot, "bots": bot_list, "venue": venue}
