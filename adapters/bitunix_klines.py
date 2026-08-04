"""Bitunix adapter, candles half: OHLC via Bitunix's public market-data REST.

Public endpoint, no API key, no signing, no coupling to the bot or to the
account adapter. Polled on its own schedule (candles don't need sub-second
refresh) and cached per-interval so a slow/failed request doesn't blank the
chart, and switching timeframes back and forth doesn't refetch every poll.

Interval support is verified empirically (see DEPLOY.md / conversation
history), not assumed from the bot's client docstring, which turned out to
be incomplete: 1s is accepted by the API but always returns zero rows (no
such candle actually exists — 1m is the real floor), while 3m and 10m work
and return genuinely distinct data despite not being documented there.
"""

from __future__ import annotations

import time

import requests

from .base import Candle

_BASE_URL = "https://fapi.bitunix.com"
_KLINE_PATH = "/api/v1/futures/market/kline"

_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 604800, "M": 2592000}

# What the UI offers. 1m is the verified floor (1s is accepted by the API
# but never has data). Order matters: this is the display order.
SUPPORTED_INTERVALS = ["1m", "3m", "10m", "15m", "30m", "1h", "2h", "6h"]
DEFAULT_INTERVAL = "1m"

# Deliberate ceiling, independent of whatever a single request returns.
# Bitunix has up to ~4.3 years of 1d history, but this dashboard only ever
# needs to show recent context. With today's SUPPORTED_INTERVALS and the
# single-page 200-candle fetch below, the coarsest (6h) only reaches ~50
# days, so this filter is currently a no-op — it's here so it stays true if
# a coarser interval (1d, 1w) or pagination is ever added later, instead of
# silently ballooning.
MAX_HISTORY_S = 182 * 86400  # ~6 months


def interval_seconds(interval: str) -> int:
    unit = interval[-1]
    n = int(interval[:-1] or 1)
    return n * _UNIT_SECONDS.get(unit, 60)


class BitunixKlineAdapter:
    def __init__(self, symbol: str, limit: int = 200, poll_interval_s: float = 20.0):
        self._symbol = symbol
        self._limit = limit
        self._poll_interval_s = poll_interval_s
        # interval -> {"candles": [...], "last_poll_ts": float}
        self._cache: dict[str, dict] = {}

    def get_klines(self, interval: str) -> list[Candle]:
        if interval not in SUPPORTED_INTERVALS:
            interval = DEFAULT_INTERVAL

        entry = self._cache.setdefault(interval, {"candles": [], "last_poll_ts": 0.0})
        now = time.time()
        if now - entry["last_poll_ts"] < self._poll_interval_s:
            return entry["candles"]
        entry["last_poll_ts"] = now

        try:
            resp = requests.get(
                _BASE_URL + _KLINE_PATH,
                params={"symbol": self._symbol, "interval": interval, "limit": self._limit},
                timeout=5.0,
            )
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError):
            return entry["candles"]  # keep last-known-good rather than blanking the chart

        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return entry["candles"]

        candles: list[Candle] = []
        for row in rows:
            try:
                candles.append(
                    {
                        "time": int(int(row["time"]) / 1000),  # ms -> s
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue

        if not candles:
            return entry["candles"]

        candles.sort(key=lambda c: c["time"])  # Bitunix returns newest-first
        cutoff = time.time() - MAX_HISTORY_S
        candles = [c for c in candles if c["time"] >= cutoff]
        entry["candles"] = candles
        return candles
