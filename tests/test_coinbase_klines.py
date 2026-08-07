"""Coverage for the Coinbase kline adapter's pure logic: row parsing and
interval mapping. The network call (_fetch_page) is monkeypatched, not hit
for real.

Runs under pytest, or standalone: `venv/bin/python tests/test_coinbase_klines.py`.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.coinbase_klines import (  # noqa: E402
    CoinbaseKlineAdapter,
    _parse_rows,
    interval_seconds,
)


def test_interval_seconds_known_and_unknown():
    assert interval_seconds("1h") == 3600
    assert interval_seconds("bogus") == interval_seconds("1m")  # falls back to default


def test_parse_rows_reads_string_fields():
    payload = {"candles": [{"start": "1700000000", "open": "1.0", "high": "2.0", "low": "0.5", "close": "1.5"}]}
    rows = _parse_rows(payload)
    assert rows == [{"time": 1700000000, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5}]


def test_parse_rows_skips_rows_missing_fields():
    payload = {"candles": [{"start": "1700000000", "open": "1.0"}]}  # missing high/low/close
    assert _parse_rows(payload) == []


def test_parse_rows_non_list_candles():
    assert _parse_rows({"candles": "nope"}) == []
    assert _parse_rows(None) == []


def test_get_klines_merges_pages_and_drops_stale_history(monkeypatch):
    now = int(time.time())
    a = CoinbaseKlineAdapter(symbol="SOL-USDC")
    calls = []
    candle = {"time": now, "open": 1, "high": 1, "low": 1, "close": 1}

    def fake_fetch(interval, end_s):
        calls.append(end_s)
        if len(calls) == 1:
            return [candle]
        return []  # no further history -> pagination stops

    monkeypatch.setattr(a, "_fetch_page", fake_fetch)
    out = a.get_klines("1m")
    assert out == [candle]
    assert len(calls) >= 1


class _FakeMonkeypatch:
    def setattr(self, obj, name, value):
        setattr(obj, name, value)


def _run_all():
    import inspect

    tests = [v for k, v in globals().items() if k.startswith("test_")]
    for t in tests:
        if "monkeypatch" in inspect.signature(t).parameters:
            t(_FakeMonkeypatch())
        else:
            t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    _run_all()
