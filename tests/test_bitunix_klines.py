"""Coverage for the Bitunix kline adapter's row parsing.

Runs under pytest, or standalone: `venv/bin/python tests/test_bitunix_klines.py`.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.bitunix_klines import BitunixKlineAdapter, _parse_rows, interval_seconds  # noqa: E402


def test_interval_seconds_known_and_unknown():
    assert interval_seconds("3m") == 180
    assert interval_seconds("2h") == 7200


def test_parse_rows_reads_quotevol_as_base_volume():
    # Verified live against fapi.bitunix.com: Bitunix's field names are
    # swapped from what they say — "quoteVol" * close ≈ "baseVol" (e.g.
    # ETHUSDT 1m: 203.739 * 1915.18 ≈ 390,197 vs baseVol 390,111.49), so
    # quoteVol actually holds the base-asset amount. Volume must come from
    # quoteVol, not baseVol, or the histogram would plot notional (~$) next
    # to venues that plot base-asset size.
    payload = {
        "data": [
            {
                "time": "1700000000000",
                "open": "1.0",
                "high": "2.0",
                "low": "0.5",
                "close": "1.5",
                "quoteVol": "10.25",
                "baseVol": "15.375",
            }
        ]
    }
    rows = _parse_rows(payload)
    assert rows == [
        {"time": 1700000000, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10.25}
    ]


def test_parse_rows_missing_volume_defaults_to_zero():
    payload = {"data": [{"time": "1700000000000", "open": "1.0", "high": "2.0", "low": "0.5", "close": "1.5"}]}
    rows = _parse_rows(payload)
    assert rows == [{"time": 1700000000, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 0.0}]


def test_parse_rows_skips_malformed_entries():
    payload = {"data": [{"time": "not-a-number", "open": "1", "high": "2", "low": "3", "close": "4"}]}
    assert _parse_rows(payload) == []


def test_parse_rows_non_list_data():
    assert _parse_rows(None) == []
    assert _parse_rows({"data": "not-a-list"}) == []


def _adapter_with_finite_history(oldest_ts: int, step: int = 60):
    """Adapter whose fake venue serves exactly 3 bars ending at `oldest_ts`
    and nothing older — the real Bitunix behaviour (~1800 1m bars) that a
    week-old bot can never page past.
    """
    a = BitunixKlineAdapter("SOLUSDT", poll_interval_s=20.0)
    calls = []

    def fake_fetch(interval, end_ms):
        calls.append(end_ms)
        if end_ms is None:  # recent page
            return [
                {"time": oldest_ts + i * step, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}
                for i in range(3)
            ]
        return []  # no history below the oldest bar, ever

    a._fetch_page = fake_fetch
    return a, calls


def test_history_floor_stops_refetching_unreachable_history():
    # since_ts a week back, venue only serves 3 bars: `target` is
    # unreachable by construction, which is exactly the case that used to
    # keep needs_history true and re-issue page requests on every call.
    now = int(time.time())
    oldest = now - 120  # 3 bars: now-120, now-60, now
    a, calls = _adapter_with_finite_history(oldest)
    since = now - 7 * 86400

    a.get_klines("1m", since_ts=since)
    first_round = len(calls)
    # 1 recent page + 1 history page that came back empty = 2.
    assert first_round == 2, calls

    # Second call, well inside poll_interval_s: must serve from cache and
    # issue no request at all. Before the floor was remembered this repeated
    # the same 2 calls every poll, forever.
    a.get_klines("1m", since_ts=since)
    a.get_klines("1m", since_ts=since)
    assert len(calls) == first_round, calls


def test_history_floor_does_not_block_the_recent_page_refresh():
    # The floor must only stop *history* paging; the in-progress candle still
    # has to refresh once the poll interval elapses, or the chart's right
    # edge freezes.
    now = int(time.time())
    a, calls = _adapter_with_finite_history(now - 120)
    since = now - 7 * 86400

    a.get_klines("1m", since_ts=since)
    calls.clear()
    a._cache["1m"]["last_poll_ts"] = 0.0  # pretend the interval elapsed
    a.get_klines("1m", since_ts=since)
    # Exactly one call, and it's the recent page (end_ms is None) — no
    # renewed attempt to page below the known floor.
    assert calls == [None], calls


def _run_all():
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("OK")


if __name__ == "__main__":
    _run_all()
