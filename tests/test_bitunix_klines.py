"""Coverage for the Bitunix kline adapter's row parsing.

Runs under pytest, or standalone: `venv/bin/python tests/test_bitunix_klines.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.bitunix_klines import _parse_rows, interval_seconds  # noqa: E402


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


def _run_all():
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("OK")


if __name__ == "__main__":
    _run_all()
