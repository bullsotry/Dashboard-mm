"""Coverage for BotStateAdapter: real temp files, no mocking of the file
layer itself — this module's whole job is reading files defensively, so the
test has to actually feed it files.

Runs under pytest, or standalone: `venv/bin/python tests/test_bot_files.py`.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.bot_files import BotStateAdapter  # noqa: E402


def approx(a, b, tol=1e-9):
    return abs(a - b) <= tol


def _adapter(tmp: Path, viz: dict | None, fills: list[dict] | None, symbol="SOLUSDT", venue="bitunix"):
    viz_path = tmp / "viz.json"
    if viz is not None:
        viz_path.write_text(json.dumps(viz))
    fills_path = None
    if fills is not None:
        fills_path = tmp / "fills.jsonl"
        fills_path.write_text("\n".join(json.dumps(f) for f in fills))
    return BotStateAdapter(venue, symbol, viz_path, fills_path)


def test_missing_viz_file_degrades_quietly():
    with tempfile.TemporaryDirectory() as d:
        a = _adapter(Path(d), viz=None, fills=None)
        assert a.get_orderbook() is None
        assert a.get_positions() == []
        assert a.get_quotes() == []
        assert a.get_source_ts() is None


def test_corrupt_json_mid_write_returns_none():
    with tempfile.TemporaryDirectory() as d:
        viz_path = Path(d) / "viz.json"
        viz_path.write_text('{"orderbook": {"mid": 100.0,')  # truncated
        a = BotStateAdapter("bitunix", "SOLUSDT", viz_path, None)
        assert a.get_orderbook() is None


def test_orderbook_read():
    viz = {
        "orderbook": {
            "bids": [[100.0, 1.0]],
            "asks": [[101.0, 2.0]],
            "best_bid": 100.0,
            "best_ask": 101.0,
            "mid": 100.5,
            "ts": 123.0,
        }
    }
    with tempfile.TemporaryDirectory() as d:
        a = _adapter(Path(d), viz=viz, fills=None)
        ob = a.get_orderbook()
        assert ob["mid"] == 100.5
        assert ob["bids"] == [{"price": 100.0, "size": 1.0}]
        assert a.get_source_ts() == 123.0


def test_positions_long_short_pair():
    viz = {
        "orderbook": {"mid": 1.0},
        "position_long": {"qty_base": 5.0, "entry_price": 10.0},
        "position_short": {"qty_base": 0.0},
    }
    with tempfile.TemporaryDirectory() as d:
        a = _adapter(Path(d), viz=viz, fills=None)
        positions = a.get_positions()
        assert len(positions) == 2
        assert positions[0]["side"] == "LONG"
        assert positions[0]["qty_base"] == 5.0


def test_positions_falls_back_to_net_position():
    viz = {"orderbook": {"mid": 1.0}, "position": {"net_position_base": -3.0}}
    with tempfile.TemporaryDirectory() as d:
        a = _adapter(Path(d), viz=viz, fills=None)
        positions = a.get_positions()
        assert len(positions) == 1
        assert positions[0]["side"] == "SHORT"
        assert positions[0]["qty_base"] == 3.0


def test_positions_net_position_zero_yields_nothing():
    viz = {"orderbook": {"mid": 1.0}, "position": {"net_position_base": 0.0}}
    with tempfile.TemporaryDirectory() as d:
        a = _adapter(Path(d), viz=viz, fills=None)
        assert a.get_positions() == []


def test_quotes_normalise_side_aliases_and_drop_other_symbols():
    viz = {
        "orderbook": {"mid": 1.0},
        "open_orders": [
            {"side": "bid", "price": 99.0, "size": 1.0},
            {"side": "ask", "price": 101.0, "qty": 2.0},
            {"symbol": "ETHUSDT", "side": "bid", "price": 50.0, "size": 1.0},  # other bot's row
            {"side": "junk", "price": 5.0, "size": 1.0},  # unrecognised side, dropped
            {"side": "buy", "price": 0.0, "size": 1.0},  # non-positive price, dropped
        ],
    }
    with tempfile.TemporaryDirectory() as d:
        a = _adapter(Path(d), viz=viz, fills=None, symbol="SOLUSDT")
        quotes = a.get_quotes()
        assert quotes == [
            {"side": "buy", "price": 99.0, "size": 1.0},
            {"side": "sell", "price": 101.0, "size": 2.0},
        ]


def test_fills_filtered_by_symbol_and_venue():
    fills = [
        {"ts": 1, "symbol": "SOLUSDT", "venue": "bitunix", "side": "buy", "price": 100.0, "size": 1.0, "fee": 0.1},
        {"ts": 2, "symbol": "SOLUSDT", "venue": "okx", "side": "buy", "price": 200.0, "size": 1.0, "fee": 0.0},
        {"ts": 3, "symbol": "ETHUSDT", "venue": "bitunix", "side": "buy", "price": 300.0, "size": 1.0, "fee": 0.0},
    ]
    with tempfile.TemporaryDirectory() as d:
        a = _adapter(Path(d), viz={"orderbook": {"mid": 1.0}}, fills=fills)
        recent = a.get_recent_fills()
        assert len(recent) == 1
        assert approx(recent[0]["price"], 100.0)


def test_fills_incremental_read_only_sees_new_lines():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        fills_path = tmp / "fills.jsonl"
        fills_path.write_text(json.dumps({"ts": 1, "symbol": "SOLUSDT", "side": "buy", "price": 1.0, "size": 1.0}) + "\n")
        a = BotStateAdapter("bitunix", "SOLUSDT", tmp / "viz.json", fills_path)
        (tmp / "viz.json").write_text(json.dumps({"orderbook": {"mid": 1.0}}))
        assert len(a.get_recent_fills()) == 1

        with open(fills_path, "a") as f:
            f.write(json.dumps({"ts": 2, "symbol": "SOLUSDT", "side": "sell", "price": 2.0, "size": 1.0}) + "\n")
        assert len(a.get_recent_fills()) == 2


def test_fills_ledger_reset_reads_from_start():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        fills_path = tmp / "fills.jsonl"
        fills_path.write_text(json.dumps({"ts": 1, "symbol": "SOLUSDT", "side": "buy", "price": 111.0, "size": 1.0}) + "\n")
        (tmp / "viz.json").write_text(json.dumps({"orderbook": {"mid": 1.0}}))
        a = BotStateAdapter("bitunix", "SOLUSDT", tmp / "viz.json", fills_path)
        assert len(a.get_recent_fills()) == 1

        # Ledger reset to a file too short to hold the old read offset — the
        # detectable half of "the ledger was replaced" (the other half,
        # inode change, is covered by test_fills_incremental_read_only_sees_new_lines'
        # sibling scenario at the OS level, not reproducible portably here).
        fills_path.write_text(json.dumps({"ts": 9, "symbol": "SOLUSDT", "side": "sell", "price": 9.0}) + "\n")
        recent = a.get_recent_fills()
        assert len(recent) == 1
        assert approx(recent[0]["price"], 9.0)


def _run_all():
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    _run_all()
