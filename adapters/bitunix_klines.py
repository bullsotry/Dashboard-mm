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


# Bitunix caps a single response at 200 rows and ignores any larger `limit`
# (verified: limit=200/500/1000/1500 all return exactly 200). Deeper history
# therefore requires walking backwards with `endTime` — camelCase; the
# snake_case spelling is silently ignored and returns page 1 again, which
# looks like "no more history" rather than like a bad parameter.
_PAGE_ROWS = 200

# History is fetched to cover the caller's requested start, but not at any
# price: 1m candles over several days is thousands of rows, and paginating
# the whole way on every refresh would mean a sustained request per second
# against a public endpoint.
MAX_CANDLES = 5000
# Deep pages fetched per call. The recent page is always refreshed; older
# pages fill in progressively over subsequent polls, so a cold start shows a
# chart immediately and extends it rather than stalling on ~20 requests.
MAX_PAGES_PER_CALL = 8


def _parse_rows(payload) -> list[Candle]:
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[Candle] = []
    for row in rows:
        try:
            out.append(
                {
                    "time": int(int(row["time"]) / 1000),  # ms -> s
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    # Bitunix's field names are swapped from what they say:
                    # verified against fapi.bitunix.com/.../kline live,
                    # "quoteVol" * close ≈ "baseVol" (e.g. ETHUSDT 1m:
                    # 203.739 * 1915.18 ≈ 390,197 vs baseVol 390,111.49).
                    # So quoteVol is actually the base-asset amount.
                    "volume": float(row.get("quoteVol") or 0.0),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


class BitunixKlineAdapter:
    # Read by the server instead of importing this module's constants, so a
    # second venue with a different granularity set doesn't get validated
    # against Bitunix's list.
    supported_intervals = SUPPORTED_INTERVALS
    default_interval = DEFAULT_INTERVAL

    @staticmethod
    def interval_seconds(interval: str) -> int:
        return interval_seconds(interval)

    def __init__(self, symbol: str, limit: int = _PAGE_ROWS, poll_interval_s: float = 20.0):
        self._symbol = symbol
        self._limit = min(limit, _PAGE_ROWS)
        self._poll_interval_s = poll_interval_s
        # interval -> {"bars": {time: Candle}, "last_poll_ts": float}
        self._cache: dict[str, dict] = {}

    def _fetch_page(self, interval: str, end_ms: int | None) -> list[Candle]:
        params = {"symbol": self._symbol, "interval": interval, "limit": self._limit}
        if end_ms is not None:
            params["endTime"] = end_ms
        try:
            resp = requests.get(_BASE_URL + _KLINE_PATH, params=params, timeout=5.0)
            resp.raise_for_status()
            return _parse_rows(resp.json())
        except (requests.RequestException, ValueError):
            return []  # keep last-known-good rather than blanking the chart

    def get_klines(self, interval: str, since_ts: float | None = None) -> list[Candle]:
        """`since_ts` is how far back the chart should reach — normally the
        bot's first known fill, so the visible history covers the whole time
        the bot has been quoting instead of an arbitrary 200 bars (3h20 on
        1m, which cut the session off mid-morning)."""
        if interval not in SUPPORTED_INTERVALS:
            interval = DEFAULT_INTERVAL

        entry = self._cache.setdefault(interval, {"bars": {}, "last_poll_ts": 0.0})
        bars: dict[int, Candle] = entry["bars"]
        now = time.time()

        step = interval_seconds(interval)
        floor_ts = now - MAX_HISTORY_S
        target = floor_ts if since_ts is None else max(float(since_ts), floor_ts)
        # Always keep a page of context, so coarse intervals aren't reduced to
        # a handful of bars just because the bot started recently.
        target = min(target, now - self._limit * step)

        needs_history = not bars or (min(bars) > target + step and len(bars) < MAX_CANDLES)
        if now - entry["last_poll_ts"] < self._poll_interval_s and not needs_history:
            return self._materialise(entry)
        entry["last_poll_ts"] = now

        # Refresh the recent page first: it carries the in-progress candle and
        # is the only page whose contents change.
        for c in self._fetch_page(interval, None):
            bars[c["time"]] = c

        pages = 0
        while bars and min(bars) > target + step and pages < MAX_PAGES_PER_CALL:
            if len(bars) >= MAX_CANDLES:
                break
            oldest_ms = min(bars) * 1000
            page = self._fetch_page(interval, oldest_ms)
            page = [c for c in page if c["time"] * 1000 < oldest_ms]
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
        out = [bars[t] for t in sorted(bars)]  # Bitunix returns newest-first
        if len(out) > MAX_CANDLES:
            out = out[-MAX_CANDLES:]
            entry["bars"] = {c["time"]: c for c in out}
        return out
