"""Coverage for bot discovery: real temp files matching real glob patterns
— the whole point of this module is filesystem scanning, so faking that
away would test nothing.

Runs under pytest, or standalone: `venv/bin/python tests/test_discovery.py`.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.discovery import discover  # noqa: E402


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def test_no_bots_found_returns_empty():
    with tempfile.TemporaryDirectory() as d:
        assert discover([f"{d}/*/*.viz.json"], []) == []


def test_discovers_bot_with_explicit_exchange_and_product_id():
    with tempfile.TemporaryDirectory() as d:
        _write(Path(d) / "bot1" / ".viz.json", {"exchange": "okx", "product_id": "SOL-USD_UM_XPERP", "orderbook": {}})
        bots = discover([f"{d}/*/.viz.json"], [])
        assert len(bots) == 1
        assert bots[0].exchange == "okx"
        assert bots[0].symbol == "SOL-USD_UM_XPERP"
        assert bots[0].key == "okx:SOL-USD_UM_XPERP"


def test_missing_orderbook_key_is_skipped():
    with tempfile.TemporaryDirectory() as d:
        _write(Path(d) / "bot1" / ".viz.json", {"exchange": "okx", "product_id": "SOL"})  # no "orderbook"
        assert discover([f"{d}/*/.viz.json"], []) == []


def test_missing_symbol_is_skipped():
    with tempfile.TemporaryDirectory() as d:
        _write(Path(d) / "bot1" / ".viz.json", {"exchange": "okx", "orderbook": {}})
        assert discover([f"{d}/*/.viz.json"], []) == []


def test_exchange_guessed_from_directory_when_absent():
    with tempfile.TemporaryDirectory() as d:
        _write(Path(d) / "bitunix_mm" / ".viz.json", {"symbol": "SOLUSDT", "orderbook": {}})
        bots = discover([f"{d}/*/.viz.json"], [])
        assert len(bots) == 1
        assert bots[0].exchange == "bitunix"  # "_mm" suffix stripped


def test_two_files_same_identity_keeps_the_fresher_one():
    with tempfile.TemporaryDirectory() as d:
        old = Path(d) / "old" / ".viz.json"
        new = Path(d) / "new" / ".viz.json"
        _write(old, {"exchange": "bitunix", "symbol": "SOLUSDT", "orderbook": {}, "marker": "old"})
        time.sleep(0.01)
        _write(new, {"exchange": "bitunix", "symbol": "SOLUSDT", "orderbook": {}, "marker": "new"})
        bots = discover([f"{d}/*/.viz.json"], [])
        assert len(bots) == 1
        assert bots[0].viz_path == new


def test_different_symbols_both_discovered_and_sorted_by_key():
    with tempfile.TemporaryDirectory() as d:
        _write(Path(d) / "ethbot" / ".viz.json", {"exchange": "bitunix", "symbol": "ETHUSDT", "orderbook": {}})
        _write(Path(d) / "solbot" / ".viz.json", {"exchange": "bitunix", "symbol": "SOLUSDT", "orderbook": {}})
        bots = discover([f"{d}/*/.viz.json"], [])
        assert [b.key for b in bots] == ["bitunix:ETHUSDT", "bitunix:SOLUSDT"]


def test_label_combines_exchange_and_symbol():
    with tempfile.TemporaryDirectory() as d:
        _write(Path(d) / "bot1" / ".viz.json", {"exchange": "coinbase", "symbol": "SOL-USDC", "orderbook": {}})
        bots = discover([f"{d}/*/.viz.json"], [])
        assert bots[0].label == "coinbase · SOL-USDC"


def test_fills_path_picks_most_recently_written():
    with tempfile.TemporaryDirectory() as d:
        older = Path(d) / "older.jsonl"
        newer = Path(d) / "newer.jsonl"
        viz_dir = Path(d) / "bot1"
        older.write_text("{}")
        time.sleep(0.01)
        newer.write_text("{}")
        _write(viz_dir / ".viz.json", {"exchange": "bitunix", "symbol": "SOLUSDT", "orderbook": {}})
        bots = discover([f"{d}/*/.viz.json"], [f"{d}/*.jsonl"])
        assert bots[0].fills_path == newer


def _run_all():
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    _run_all()
