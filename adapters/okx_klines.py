"""OKX adapter, candles half: OHLC via OKX's public market-data REST.

Same contract as the Bitunix/Coinbase kline adapters — `get_klines(interval,
since_ts)` returning oldest-first candles, cached per interval, a failed
request keeps the last known history instead of blanking the chart.

OKX is not a plain symbol->endpoint mapping like the other two venues. The
account this dashboard watches trades FUTURES named `<BASE>-USD_UM_XPERP`,
not the SWAP instruments OKX docs default to (the EEA account has zero SWAP
instruments available to it — see v17mm OKX's okx_client.py, which this
adapter's `_inst_family` mirrors). The instrument id itself carries a
rolling expiry suffix (`SOL-USD_UM_XPERP-310404`) that cannot be built by
string formatting and must be resolved against OKX per request, which is
why this adapter has a resolve step the other two don't.

Both calls used here are public (`/api/v5/public/...`, `/api/v5/market/...`)
— no key, no signing, no coupling to the bot's own OKXClient or its
credentials, matching the no-auth posture of the Bitunix/Coinbase adapters.
"""

from __future__ import annotations

import time

import requests

from ._locking import CacheGuard
from .base import Candle

_BASE_URL = "https://www.okx.com"
_INSTRUMENTS_PATH = "/api/v5/public/instruments"
_CANDLES_PATH = "/api/v5/market/candles"

_INST_TYPE = "FUTURES"
_XPERP_SUFFIX = "-USD_UM_XPERP"
_QUOTE_CCYS = ("USDT", "USDC", "USD")

# OKX bar codes differ from our lowercase interval spelling for the coarse
# buckets (1H/2H/6H are uppercase on OKX) — mirrors _OKX_BAR in the bot's
# own okx_client.py so the two never drift into disagreeing about candles
# for the same instrument.
_OKX_BAR = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1H", "2h": "2H", "6h": "6H",
}

SUPPORTED_INTERVALS = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "6h"]
DEFAULT_INTERVAL = "1m"

_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400}


def interval_seconds(interval: str) -> int:
    unit = interval[-1]
    n = int(interval[:-1] or 1)
    return n * _UNIT_SECONDS.get(unit, 60)


# OKX caps /market/candles at 300 rows per request (default 100); deeper
# history is paginated the same way as the other two adapters, walking
# `after` backwards from the oldest bar held so far.
_PAGE_ROWS = 100
MAX_HISTORY_S = 182 * 86400  # ~6 months, matches the other two adapters
MAX_CANDLES = 5000
MAX_PAGES_PER_CALL = 8

# How long a resolved instId is trusted before re-checking OKX. Matches the
# bot's own instrument_cache_ttl_s ballpark: the expiry suffix rolls rarely,
# so this is about tolerating a rollover within a session, not chasing it
# live.
_INST_CACHE_TTL_S = 300.0


def _inst_family(symbol: str) -> str:
    """"SOL" / "SOL-USDT" / "SOLUSDT" / "SOL-USDC" -> "SOL-USD_UM_XPERP".

    Deliberately drops the quote currency: every family this account can
    trade is USD-denominated and USDC-settled, so "SOL-USDT" and "SOL-USDC"
    both name the one SOL contract. Ported from okx_inst_family in v17mm
    OKX's okx_client.py — keep the two in sync if that logic changes.
    """
    s = str(symbol or "").strip().upper().replace("/", "-")
    if not s:
        return s
    if "_UM_XPERP" in s:
        head, _, tail = s.rpartition("-")
        return head if head.endswith("_UM_XPERP") and tail.isdigit() else s
    s = s.replace("_", "-")
    if s.endswith("-SWAP"):
        s = s[: -len("-SWAP")]
    base = s
    if "-" in s:
        base = s.split("-", 1)[0]
    else:
        for quote in _QUOTE_CCYS:
            if s.endswith(quote) and len(s) > len(quote):
                base = s[: -len(quote)]
                break
    return f"{base}{_XPERP_SUFFIX}"


def _parse_rows(payload) -> list[Candle]:
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[Candle] = []
    for row in rows:
        try:
            out.append(
                {
                    "time": int(int(row[0]) / 1000),  # ms -> s
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),  # base-ccy volume; row[6] is quote-ccy
                }
            )
        except (IndexError, TypeError, ValueError):
            continue
    return out


class OkxKlineAdapter:
    supported_intervals = SUPPORTED_INTERVALS
    default_interval = DEFAULT_INTERVAL

    @staticmethod
    def interval_seconds(interval: str) -> int:
        return interval_seconds(interval)

    def __init__(self, symbol: str, poll_interval_s: float = 20.0):
        self._family = _inst_family(symbol)
        self._poll_interval_s = poll_interval_s
        # One in-flight fetch per adapter, and no lock held across it.
        self._guard = CacheGuard()
        self._inst_id: str | None = None
        self._inst_resolved_ts = 0.0
        # interval -> {"bars": {time: Candle}, "last_poll_ts": float}
        self._cache: dict[str, dict] = {}

    def _resolve_inst_id(self) -> str | None:
        """Live instId for our family, re-checked every _INST_CACHE_TTL_S.

        Public endpoint only: this adapter never sees the bot's API keys,
        so unlike OKXClient._inst_id there is no account-scoped fallback —
        just the one public lookup that already covers every unauthenticated
        caller.
        """
        now = time.time()
        if self._inst_id is not None and (now - self._inst_resolved_ts) < _INST_CACHE_TTL_S:
            return self._inst_id
        try:
            resp = requests.get(
                _BASE_URL + _INSTRUMENTS_PATH,
                params={"instType": _INST_TYPE, "instFamily": self._family},
                timeout=5.0,
            )
            resp.raise_for_status()
            rows = resp.json().get("data") or []
        except (requests.RequestException, ValueError):
            return self._inst_id  # keep last-known-good id over a transient failure

        live = [r for r in rows if str(r.get("state") or "live") == "live"]
        if not live:
            return self._inst_id
        if len(live) > 1:
            live.sort(key=lambda r: int(str(r.get("expTime") or 0) or 0), reverse=True)
        inst_id = str(live[0].get("instId") or "")
        if inst_id:
            self._inst_id = inst_id
            self._inst_resolved_ts = now
        return self._inst_id

    def _fetch_page(self, interval: str, after_ms: int | None) -> list[Candle]:
        inst_id = self._resolve_inst_id()
        if not inst_id:
            return []
        params = {"instId": inst_id, "bar": _OKX_BAR.get(interval, interval), "limit": _PAGE_ROWS}
        if after_ms is not None:
            params["after"] = after_ms
        try:
            resp = requests.get(_BASE_URL + _CANDLES_PATH, params=params, timeout=5.0)
            resp.raise_for_status()
            return _parse_rows(resp.json())
        except (requests.RequestException, ValueError):
            return []  # keep last-known-good rather than blanking the chart

    def get_klines(
        self,
        interval: str,
        since_ts: float | None = None,
        serve_only: bool = False,
    ) -> list[Candle]:
        """Candles for this interval.

        `serve_only` is what /snapshot passes: answer from cache, never make
        the caller wait on this venue. Up to MAX_PAGES_PER_CALL pages at a 5s
        timeout means a request could block for the better part of a minute
        on a backfill — the same stall the account warm loop was written to
        remove, left in place here. server.py's kline warm loop now owns the
        fetching; the request path only reads what that loop has already put
        in the cache.

        The one exception is a genuinely cold cache, where serving nothing
        would mean a blank chart until the next warm tick. There it fetches
        the recent page and only that: one round trip instead of eight, so a
        cold start is *faster* than it was, and the warm loop fills in the
        history behind it.
        """
        if interval not in _OKX_BAR:
            interval = DEFAULT_INTERVAL

        with self._guard.data:
            entry = self._cache.setdefault(interval, {"bars": {}, "last_poll_ts": 0.0})
            bars: dict[int, Candle] = entry["bars"]
            now = time.time()

            step = interval_seconds(interval)
            floor_ts = now - MAX_HISTORY_S
            target = floor_ts if since_ts is None else max(float(since_ts), floor_ts)
            target = min(target, now - _PAGE_ROWS * step)

            # See bitunix_klines.get_klines for why this floor exists: a venue's
            # history is finite, so a bot older than it can never reach `target`,
            # and without remembering the floor `needs_history` stays true
            # forever and defeats the poll-interval cache on every request.
            history_floor = entry.get("history_floor")
            at_floor = history_floor is not None and bars and min(bars) <= history_floor
            needs_history = not bars or (
                min(bars) > target + step and len(bars) < MAX_CANDLES and not at_floor
            )
            # Cache is populated: serve it and let the warm loop do the
            # fetching. `needs_history` is deliberately ignored — extending
            # the chart backwards is worth a background round trip, never a
            # request the operator is waiting on.
            if serve_only and bars:
                return self._materialise(entry)

            if now - entry["last_poll_ts"] < self._poll_interval_s and not needs_history:
                return self._materialise(entry)
            entry["last_poll_ts"] = now

        # Everything below fetches. The data lock is taken only to merge a
        # page that has already arrived — never held across a request, or a
        # concurrent reader would wait out the whole backfill (up to 8 pages
        # at a 5s timeout) for candles it already has. See adapters/_locking.
        with self._guard.try_fetch() as fetching:
            if not fetching:
                # Another thread is already refreshing this interval. Serve
                # what we hold instead of firing a duplicate set of REST
                # calls at the venue.
                with self._guard.data:
                    return self._materialise(entry)

            # Recent page first: it carries the in-progress candle.
            page = self._fetch_page(interval, None)
            with self._guard.data:
                for c in page:
                    bars[c["time"]] = c

            pages = 0
            # `not at_floor` here too — see bitunix_klines for why the loop
            # must respect the floor, not just the cache check above.
            # serve_only reaching here means the cache was empty: take the
            # recent page above and stop, rather than paging history on a
            # thread the browser is blocked on.
            max_pages = 0 if serve_only else MAX_PAGES_PER_CALL
            while pages < max_pages and not at_floor:
                with self._guard.data:
                    if not bars or len(bars) >= MAX_CANDLES or min(bars) <= target + step:
                        break
                    oldest_ms = min(bars) * 1000
                page = [
                    c for c in self._fetch_page(interval, oldest_ms)
                    if c["time"] * 1000 < oldest_ms
                ]
                with self._guard.data:
                    if not page:
                        # Venue has no more history below this point.
                        entry["history_floor"] = min(bars)
                        break
                    for c in page:
                        bars[c["time"]] = c
                pages += 1

        with self._guard.data:
            return self._materialise(entry)
        entry["last_poll_ts"] = now

        # Recent page first: it carries the in-progress candle.
        for c in self._fetch_page(interval, None):
            bars[c["time"]] = c

        pages = 0
        # `not at_floor` here too — see bitunix_klines for why the loop
        # must respect the floor, not just the cache check above.
        while bars and min(bars) > target + step and pages < MAX_PAGES_PER_CALL and not at_floor:
            if len(bars) >= MAX_CANDLES:
                break
            oldest_ms = min(bars) * 1000
            page = [c for c in self._fetch_page(interval, oldest_ms) if c["time"] * 1000 < oldest_ms]
            if not page:
                entry["history_floor"] = min(bars)  # venue has no more history
                break
            for c in page:
                bars[c["time"]] = c
            pages += 1

        return self._materialise(entry)

    def _materialise(self, entry: dict) -> list[Candle]:
        bars: dict[int, Candle] = entry["bars"]
        cutoff = time.time() - MAX_HISTORY_S
        for t in [t for t in bars if t < cutoff]:
            del bars[t]
        out = [bars[t] for t in sorted(bars)]
        if len(out) > MAX_CANDLES:
            out = out[-MAX_CANDLES:]
            entry["bars"] = {c["time"]: c for c in out}
        return out
