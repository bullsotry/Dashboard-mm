"""Coverage for the OKX kline adapter's pure logic: instrument-family
resolution (the trickiest part — SOL / SOL-USDT / SOLUSDT / an already-rolled
instId all have to land on the same family) and row parsing. The network
calls (_resolve_inst_id, _fetch_page) are exercised separately via
monkeypatching, not hit for real.

Runs under pytest, or standalone: `venv/bin/python tests/test_okx_klines.py`.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.okx_klines import (  # noqa: E402
    OkxKlineAdapter,
    _inst_family,
    _parse_rows,
    interval_seconds,
)


def test_inst_family_plain_base():
    assert _inst_family("SOL") == "SOL-USD_UM_XPERP"


def test_inst_family_strips_usdt_quote():
    assert _inst_family("SOLUSDT") == "SOL-USD_UM_XPERP"


def test_inst_family_strips_dashed_quote():
    assert _inst_family("SOL-USDC") == "SOL-USD_UM_XPERP"


def test_inst_family_strips_swap_suffix():
    assert _inst_family("SOL-USD-SWAP") == "SOL-USD_UM_XPERP"


def test_inst_family_already_resolved_instid_passes_through():
    assert _inst_family("SOL-USD_UM_XPERP-310404") == "SOL-USD_UM_XPERP"


def test_inst_family_empty_symbol():
    assert _inst_family("") == ""
    assert _inst_family(None) == ""


def test_inst_family_case_and_slash_normalised():
    assert _inst_family("sol/usdt") == "SOL-USD_UM_XPERP"


def test_interval_seconds():
    assert interval_seconds("1m") == 60
    assert interval_seconds("2h") == 7200
    assert interval_seconds("6h") == 21600


def test_parse_rows_converts_ms_to_s_and_types():
    payload = {"data": [["1700000000000", "1.0", "2.0", "0.5", "1.5", "10.25"]]}
    rows = _parse_rows(payload)
    assert rows == [
        {"time": 1700000000, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10.25}
    ]


def test_parse_rows_skips_malformed_entries():
    payload = {
        "data": [
            ["not-a-number", "1", "2", "3", "4", "5"],
            ["1700000000000", "1", "2", "3", "4", "5"],
        ]
    }
    rows = _parse_rows(payload)
    assert len(rows) == 1


def test_parse_rows_missing_volume_index_skips_row():
    # A row shorter than expected (no volume column) is malformed, not
    # "volume unknown" — dropped like any other truncated row, not silently
    # defaulted to 0, which would understate a real bar's volume.
    payload = {"data": [["1700000000000", "1.0", "2.0", "0.5", "1.5"]]}
    assert _parse_rows(payload) == []


def test_parse_rows_non_dict_payload():
    assert _parse_rows(None) == []
    assert _parse_rows({"data": "not-a-list"}) == []


def test_get_klines_dedupes_by_time_and_sorts(monkeypatch):
    # Timestamps must be near "now" — _materialise() prunes anything older
    # than MAX_HISTORY_S, and small hand-picked ints like 100/200 read as
    # 1970, so they'd be pruned before the assertion ever sees them.
    now = int(time.time())
    t_a, t_b = now - 60, now
    a = OkxKlineAdapter(symbol="SOL")
    monkeypatch.setattr(a, "_fetch_page", lambda interval, after_ms: [
        {"time": t_b, "open": 1, "high": 1, "low": 1, "close": 1},
        {"time": t_a, "open": 2, "high": 2, "low": 2, "close": 2},
        {"time": t_b, "open": 3, "high": 3, "low": 3, "close": 3},  # overwrites the first t_b
    ])
    out = a.get_klines("1m")
    assert [c["time"] for c in out] == [t_a, t_b]
    assert out[1]["open"] == 3


def test_get_klines_unsupported_interval_falls_back_to_default(monkeypatch):
    # A single cached bar is always "needs_history" as far as get_klines is
    # concerned (it wants a full page of context), so it will try to fetch
    # regardless of the poll-interval throttle. _fetch_page is stubbed to
    # return nothing rather than left alone, so this test proves the
    # interval-fallback routing without reaching out to OKX for real.
    now = int(time.time())
    a = OkxKlineAdapter(symbol="SOL")
    a._cache["1m"] = {"bars": {now: {"time": now, "open": 1, "high": 1, "low": 1, "close": 1}}, "last_poll_ts": 1e18}
    monkeypatch.setattr(a, "_fetch_page", lambda interval, after_ms: [])
    out = a.get_klines("99z")
    assert len(out) == 1  # served from the "1m" (default) cache entry


def _run_all():
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    for t in tests:
        import inspect

        if "monkeypatch" in inspect.signature(t).parameters:
            t(_FakeMonkeypatch())
        else:
            t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


class _FakeMonkeypatch:
    """Minimal stand-in for pytest's monkeypatch fixture, so this file also
    runs standalone without pytest installed."""

    def setattr(self, obj, name, value):
        setattr(obj, name, value)


if __name__ == "__main__":
    _run_all()
