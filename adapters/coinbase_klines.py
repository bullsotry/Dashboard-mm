"""Coinbase adapter, candles half: OHLC via the public Advanced Trade API.

Same contract as the Bitunix kline adapter — `get_klines(interval, since_ts)`
returning oldest-first candles, cached per interval so switching timeframes
back and forth doesn't refetch, and a failed request keeps the last known
history instead of blanking the chart.

Endpoint choice is not arbitrary. The Exchange (Pro) API, the obvious first
guess, does not serve this bot's product at all: `/products/SOL-USDC/candles`
returns `{"message":"NotFound"}`, because SOL-USDC is an Advanced Trade
product. The `/api/v3/brokerage/market/...` path is the *public* mirror of
the brokerage API — no key, no signing — which keeps this adapter as
uncoupled from credentials as its Bitunix counterpart.

Verified against the live endpoint: a page is capped at 349 candles
("number of candles requested should be less than 350"), the request is
rejected outright rather than truncated when the start/end span asks for
more, and rows come back newest-first with every field as a string.
"""

from __future__ import annotations

import time

import requests

from .base import Candle

_URL = "https://api.coinbase.com/api/v3/brokerage/market/products/{product}/candles"

# Coinbase takes a named granularity, not a duration, so the mapping is the
# whitelist: an interval absent from here cannot be requested at all.
_GRANULARITY = {
    "1m": ("ONE_MINUTE", 60),
    "5m": ("FIVE_MINUTE", 300),
    "15m": ("FIFTEEN_MINUTE", 900),
    "30m": ("THIRTY_MINUTE", 1800),
    "1h": ("ONE_HOUR", 3600),
    "2h": ("TWO_HOUR", 7200),
    "6h": ("SIX_HOUR", 21600),
    "1d": ("ONE_DAY", 86400),
}

# Display order in the UI.
SUPPORTED_INTERVALS = ["1m", "5m", "15m", "30m", "1h", "2h", "6h", "1d"]
DEFAULT_INTERVAL = "1m"

# Under the venue's own 349 ceiling, with room to spare: the span is computed
# from wall-clock time, and a request that lands one candle over the line is
# rejected entirely rather than trimmed.
_PAGE_ROWS = 300

MAX_HISTORY_S = 182 * 86400  # ~6 months, matches the Bitunix adapter
MAX_CANDLES = 5000
# Deep pages per call: the recent page always refreshes, older pages fill in
# over subsequent polls so a cold start draws a chart immediately.
MAX_PAGES_PER_CALL = 8


def interval_seconds(interval: str) -> int:
    return _GRANULARITY.get(interval, _GRANULARITY[DEFAULT_INTERVAL])[1]


def _parse_rows(payload) -> list[Candle]:
    rows = payload.get("candles") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[Candle] = []
    for row in rows:
        try:
            out.append(
                {
                    "time": int(row["start"]),  # already seconds
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


class CoinbaseKlineAdapter:
    supported_intervals = SUPPORTED_INTERVALS
    default_interval = DEFAULT_INTERVAL

    def __init__(self, symbol: str, poll_interval_s: float = 20.0):
        self._symbol = symbol
        self._poll_interval_s = poll_interval_s
        # interval -> {"bars": {time: Candle}, "last_poll_ts": float}
        self._cache: dict[str, dict] = {}

    @staticmethod
    def interval_seconds(interval: str) -> int:
        return interval_seconds(interval)

    def _fetch_page(self, interval: str, end_s: int) -> list[Candle]:
        """One page ending at `end_s`. Coinbase pages by an explicit
        [start, end] window rather than by a cursor, so the caller walks
        backwards by moving `end` to the oldest bar it already holds."""
        step = interval_seconds(interval)
        params = {
            "granularity": _GRANULARITY[interval][0],
            "start": str(int(end_s - _PAGE_ROWS * step)),
            "end": str(int(end_s)),
            "limit": _PAGE_ROWS,
        }
        try:
            resp = requests.get(_URL.format(product=self._symbol), params=params, timeout=5.0)
            resp.raise_for_status()
            return _parse_rows(resp.json())
        except (requests.RequestException, ValueError):
            return []  # keep last-known-good rather than blanking the chart

    def get_klines(self, interval: str, since_ts: float | None = None) -> list[Candle]:
        if interval not in _GRANULARITY:
            interval = DEFAULT_INTERVAL

        entry = self._cache.setdefault(interval, {"bars": {}, "last_poll_ts": 0.0})
        bars: dict[int, Candle] = entry["bars"]
        now = time.time()

        step = interval_seconds(interval)
        floor_ts = now - MAX_HISTORY_S
        target = floor_ts if since_ts is None else max(float(since_ts), floor_ts)
        # Always keep a page of context, so a bot that started ten minutes
        # ago still gets a chart with something to read.
        target = min(target, now - _PAGE_ROWS * step)

        needs_history = not bars or (min(bars) > target + step and len(bars) < MAX_CANDLES)
        if now - entry["last_poll_ts"] < self._poll_interval_s and not needs_history:
            return self._materialise(entry)
        entry["last_poll_ts"] = now

        # The recent page carries the in-progress candle: refresh it first so
        # the chart's right edge is live even when history is still filling.
        for c in self._fetch_page(interval, int(now) + step):
            bars[c["time"]] = c

        pages = 0
        while bars and min(bars) > target + step and pages < MAX_PAGES_PER_CALL:
            if len(bars) >= MAX_CANDLES:
                break
            oldest = min(bars)
            page = [c for c in self._fetch_page(interval, oldest) if c["time"] < oldest]
            if not page:
                break  # venue has no more history; stop rather than loop
            for c in page:
                bars[c["time"]] = c
            pages += 1

        return self._materialise(entry)

    def _materialise(self, entry: dict) -> list[Candle]:
        bars: dict[int, Candle] = entry["bars"]
        cutoff = time.time() - MAX_HISTORY_S
        for t in [t for t in bars if t < cutoff]:
            del bars[t]
        out = [bars[t] for t in sorted(bars)]  # Coinbase returns newest-first
        if len(out) > MAX_CANDLES:
            out = out[-MAX_CANDLES:]
            entry["bars"] = {c["time"]: c for c in out}
        return out
