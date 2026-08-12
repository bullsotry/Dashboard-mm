"""Coverage for BotStateAdapter: real temp files, no mocking of the file
layer itself — this module's whole job is reading files defensively, so the
test has to actually feed it files.

Runs under pytest, or standalone: `venv/bin/python tests/test_bot_files.py`.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
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


def test_negative_venue_fee_is_read_as_a_cost_not_income():
    """OKX writes a charged fee as a negative number; Bitunix/Coinbase write
    it positive. Ingesting the raw sign made `realised_net = realised - fees`
    *add* OKX's fees to the PnL.

    Hand-computed on the real 2026-08-12 window, scaled to two round trips:
    buy 1 @ 75.00, sell 1 @ 75.10 -> gross +0.10; four fills' worth of fee at
    -0.0030284 each (OKX's own sign) = 0.01211360 of cost.
    net = 0.10 - 0.0121136 = 0.0878864.
    Read raw, it would have been 0.10 + 0.0121136 = 0.1121136 — a 27.6%
    overstatement here, and a sign flip on the live window (+5.1953 shown
    against a true -0.7997).
    """
    fee = -0.0030284
    fills = [
        {"ts": 1, "symbol": "SOL-USDT", "venue": "okx", "side": "buy", "price": 75.00, "size": 0.5, "fee": fee},
        {"ts": 2, "symbol": "SOL-USDT", "venue": "okx", "side": "buy", "price": 75.00, "size": 0.5, "fee": fee},
        {"ts": 3, "symbol": "SOL-USDT", "venue": "okx", "side": "sell", "price": 75.10, "size": 0.5, "fee": fee},
        {"ts": 4, "symbol": "SOL-USDT", "venue": "okx", "side": "sell", "price": 75.10, "size": 0.5, "fee": fee},
    ]
    with tempfile.TemporaryDirectory() as d:
        a = _adapter(Path(d), viz={"orderbook": {"mid": 75.05}}, fills=fills, symbol="SOL-USDT", venue="okx")
        for f in a.get_recent_fills():
            assert f["fee"] > 0, "a fee must reach the engine as a cost"
        stats = a.get_stats()
        assert approx(stats["fees"], 0.01211360, tol=1e-9)
        assert approx(stats["realised_gross"], 0.10, tol=1e-9)
        assert approx(stats["realised_net"], 0.08788640, tol=1e-9)


def test_positive_venue_fee_is_unchanged():
    # The Bitunix/Coinbase convention must survive the normalisation
    # untouched: buy 1 @ 100, sell 1 @ 101 -> gross +1.00, fees 0.2,
    # net +0.80.
    fills = [
        {"ts": 1, "symbol": "SOLUSDT", "venue": "bitunix", "side": "buy", "price": 100.0, "size": 1.0, "fee": 0.1},
        {"ts": 2, "symbol": "SOLUSDT", "venue": "bitunix", "side": "sell", "price": 101.0, "size": 1.0, "fee": 0.1},
    ]
    with tempfile.TemporaryDirectory() as d:
        a = _adapter(Path(d), viz={"orderbook": {"mid": 100.5}}, fills=fills)
        stats = a.get_stats()
        assert approx(stats["fees"], 0.2)
        assert approx(stats["realised_gross"], 1.0)
        assert approx(stats["realised_net"], 0.8)


def test_equity_curve_reads_position_for_reliability_and_tracks_fills():
    fills = [
        {"ts": 1, "symbol": "SOLUSDT", "venue": "bitunix", "side": "buy", "price": 100.0, "size": 1.0, "fee": 0.0},
        {"ts": 2, "symbol": "SOLUSDT", "venue": "bitunix", "side": "sell", "price": 101.0, "size": 1.0, "fee": 0.0},
    ]
    with tempfile.TemporaryDirectory() as d:
        # Exchange agrees with the replay (flat after both fills) -> reliable.
        a = _adapter(Path(d), viz={"orderbook": {"mid": 1.0}, "position": {"net_position_base": 0.0}}, fills=fills)
        curve = a.get_equity_curve()
        assert [p["ts"] for p in curve] == [1, 2]
        assert approx(curve[-1]["realised_net"], 1.0)
        assert curve[-1]["inventory_base"] == 0.0
        assert curve[-1]["pnl_unreliable"] is None


def test_equity_curve_flags_hedge_mode_same_as_get_stats():
    fills = [
        {"ts": 1, "symbol": "SOLUSDT", "venue": "bitunix", "side": "buy", "price": 100.0, "size": 1.0, "fee": 0.0},
    ]
    viz = {
        "orderbook": {"mid": 1.0},
        "position_long": {"qty_base": 1.0},
        "position_short": {"qty_base": 1.0},
    }
    with tempfile.TemporaryDirectory() as d:
        a = _adapter(Path(d), viz=viz, fills=fills)
        stats = a.get_stats()
        curve = a.get_equity_curve()
        assert stats["pnl_unreliable"] is not None
        assert curve[-1]["pnl_unreliable"] == stats["pnl_unreliable"]


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


def test_stats_cache_returns_same_numbers_and_invalidates_on_new_fills():
    # The caches behind get_stats/get_equity_curve must be indistinguishable
    # from recomputing: same figures on a hit, and a new fill must change
    # them. A stale realised PnL is exactly the confident lie this dashboard
    # exists to avoid, so this is the test that matters for them.
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        fills_path = tmp / "fills.jsonl"
        # buy 1 @ 10, sell 1 @ 12 -> realised gross +2.00, flat afterwards.
        fills_path.write_text(
            json.dumps({"ts": 1, "symbol": "SOLUSDT", "side": "buy", "price": 10.0, "size": 1.0}) + "\n"
            + json.dumps({"ts": 2, "symbol": "SOLUSDT", "side": "sell", "price": 12.0, "size": 1.0}) + "\n"
        )
        (tmp / "viz.json").write_text(json.dumps({"orderbook": {"mid": 11.0}, "position": {"net_position_base": 0.0}}))
        a = BotStateAdapter("bitunix", "SOLUSDT", tmp / "viz.json", fills_path)

        first = a.get_stats()
        assert approx(first["realised_gross"], 2.0)  # 12 - 10, one round trip
        second = a.get_stats()  # served from cache
        assert second == first
        assert len(a.get_equity_curve()) == 2

        # A third fill: sell 1 @ 15 with nothing left to match opens a short,
        # so realised gross is unchanged but the curve gains a point. The
        # cache must not hide either fact.
        with open(fills_path, "a") as f:
            f.write(json.dumps({"ts": 3, "symbol": "SOLUSDT", "side": "sell", "price": 15.0, "size": 1.0}) + "\n")
        assert len(a.get_equity_curve()) == 3
        assert a.get_stats()["n_fills"] == 3


def _atomic_write(path: Path, text: str) -> None:
    """Replace a file the way the bot does — mkstemp + os.replace — so the
    inode changes, which is what `_read_viz`'s cache identity relies on.

    Writing in place instead made this suite flaky on a fast host: two
    consecutive writes of equal length keep the same inode and size, and can
    land on the *same* `st_mtime_ns` (measured on the VPS: delta 0ns), so the
    cache correctly saw nothing had changed. The bot never writes that way
    (bitunix_mm/strategy.py publishes via os.replace), so simulating it was
    testing a scenario production cannot produce.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def test_stats_cache_invalidates_when_net_position_changes():
    # Same fills, different exchange-reported position: pnl_unreliable is
    # derived from comparing the two, so a cache keyed only on fills would
    # keep serving a reliability verdict that no longer holds.
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        fills_path = tmp / "fills.jsonl"
        fills_path.write_text(
            json.dumps({"ts": 1, "symbol": "SOLUSDT", "side": "buy", "price": 10.0, "size": 1.0}) + "\n"
        )
        viz = tmp / "viz.json"
        # Replay implies +1 base held; exchange agrees -> reliable.
        viz.write_text(json.dumps({"orderbook": {"mid": 10.0}, "position": {"net_position_base": 1.0}}))
        a = BotStateAdapter("bitunix", "SOLUSDT", viz, fills_path)
        assert not a.get_stats().get("pnl_unreliable")

        # Exchange now reports a different position, fills untouched.
        _atomic_write(viz, json.dumps({"orderbook": {"mid": 10.0}, "position": {"net_position_base": 7.0}}))
        assert a.get_stats().get("pnl_unreliable")


def test_viz_cache_reflects_a_rewritten_file():
    # _read_viz caches on the file's stat identity; a rewrite must be seen.
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        viz = tmp / "viz.json"
        viz.write_text(json.dumps({"orderbook": {"mid": 1.0, "best_bid": 0.9, "best_ask": 1.1}}))
        a = BotStateAdapter("bitunix", "SOLUSDT", viz, None)
        assert approx(a.get_orderbook()["mid"], 1.0)
        assert approx(a.get_orderbook()["mid"], 1.0)  # cache hit, same answer

        time.sleep(0.01)  # ensure a distinct mtime_ns on coarse-clock systems
        viz.write_text(json.dumps({"orderbook": {"mid": 2.0, "best_bid": 1.9, "best_ask": 2.1}}))
        assert approx(a.get_orderbook()["mid"], 2.0)


def test_fills_merged_across_multiple_ledgers_and_sorted_by_ts():
    # Reproduces the real fleet shape: one tracker writes fills.jsonl for
    # bitunix+coinbase, a separate tracker writes fills_okx.jsonl for OKX.
    # An OKX bot's adapter must see rows from *its* ledger even though a
    # different, unrelated ledger also matches the glob.
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        shared = tmp / "fills.jsonl"
        okx = tmp / "fills_okx.jsonl"
        shared.write_text(
            json.dumps({"ts": 1, "symbol": "SOL-USDT", "venue": "bitunix", "side": "buy", "price": 1.0, "size": 1.0})
            + "\n"
        )
        okx.write_text(
            json.dumps({"ts": 2, "symbol": "SOL-USDT", "venue": "okx", "side": "sell", "price": 2.0, "size": 1.0})
            + "\n"
        )
        (tmp / "viz.json").write_text(json.dumps({"orderbook": {"mid": 1.0}}))
        a = BotStateAdapter("okx", "SOL-USDT", tmp / "viz.json", [shared, okx])
        recent = a.get_recent_fills()
        assert len(recent) == 1
        assert approx(recent[0]["price"], 2.0)


def test_fills_reset_on_one_ledger_does_not_touch_another():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        a_path = tmp / "a.jsonl"
        b_path = tmp / "b.jsonl"
        a_path.write_text(json.dumps({"ts": 1, "symbol": "SOLUSDT", "venue": "bitunix", "side": "buy", "price": 1.0, "size": 1.0}) + "\n")
        b_path.write_text(json.dumps({"ts": 2, "symbol": "SOLUSDT", "venue": "bitunix", "side": "sell", "price": 2.0, "size": 1.0}) + "\n")
        (tmp / "viz.json").write_text(json.dumps({"orderbook": {"mid": 1.0}}))
        adapter = BotStateAdapter("bitunix", "SOLUSDT", tmp / "viz.json", [a_path, b_path])
        assert len(adapter.get_recent_fills()) == 2

        # b_path resets to a file too short to hold the old read offset (the
        # detectable half of "the ledger was replaced" — see
        # test_fills_ledger_reset_reads_from_start for the inode-change half).
        b_path.write_text(json.dumps({"ts": 9, "symbol": "SOLUSDT", "side": "sell", "price": 9.0}) + "\n")
        recent = adapter.get_recent_fills()
        prices = sorted(f["price"] for f in recent)
        assert approx(prices[0], 1.0)  # a_path's fill survived
        assert approx(prices[1], 9.0)  # b_path re-read from its new start


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




# --------------------------------------------------------------------------
# contract_value: the OKX engine counts contracts, the panel shows base units
# --------------------------------------------------------------------------


def _okx_fill(ts, seq, inv_before, size=0.06, cv=0.01):
    return {
        "ts": ts, "venue": "okx", "symbol": "SOL-USDT", "side": "buy",
        "price": 75.7, "size": size, "fee": 0.0,
        "inventory_before": inv_before, "contract_value": cv,
        "fill_seq": seq, "fill_run_id": 1,
    }


def test_position_is_converted_to_base_with_the_ledgers_contract_value():
    # The live OKX shape: viz says net_position_base 2489, the account holds
    # 24.89 SOL (ctVal 0.01, declared by the tracker on every fill row).
    # 2489 * 0.01 = 24.89.
    with tempfile.TemporaryDirectory() as d:
        a = _adapter(
            Path(d),
            viz={"position": {"net_position_base": 2489.0, "entry_price": 75.75}},
            fills=[_okx_fill(1.0, 1, 2483.0)],
            symbol="SOL-USDT",
            venue="okx",
        )
        pos = a.get_positions()
        assert len(pos) == 1
        assert approx(pos[0]["qty_base"], 24.89)
        net, _hedge = a._net_and_hedge()
        assert approx(net, 24.89)


def test_a_ledger_that_declares_no_contract_value_is_left_alone():
    # Bitunix: no contract_value on the rows, so nothing is scaled and the
    # position reads exactly what the bot published.
    with tempfile.TemporaryDirectory() as d:
        a = _adapter(
            Path(d),
            viz={"position": {"net_position_base": 1.63}},
            fills=[{
                "ts": 1.0, "venue": "bitunix", "symbol": "SOLUSDT", "side": "buy",
                "price": 75.8, "size": 0.14, "fee": 0.0,
            }],
        )
        net, _ = a._net_and_hedge()
        assert approx(net, 1.63)
        assert approx(a.get_positions()[0]["qty_base"], 1.63)


def test_converted_position_lets_a_matching_replay_through():
    # End to end, in the units the panel actually compares: the ledger's
    # fills sum to 24.89 base (0.06 * 414 + ... — here one fill of 24.89 to
    # keep the arithmetic visible), the viz reports 2489 contracts, and with
    # the conversion the two agree, so nothing is refused.
    with tempfile.TemporaryDirectory() as d:
        a = _adapter(
            Path(d),
            viz={"position": {"net_position_base": 2489.0}},
            fills=[_okx_fill(1.0, 1, 0.0, size=24.89)],
            symbol="SOL-USDT",
            venue="okx",
        )
        s = a.get_stats()
        assert approx(s["inventory_base"], 24.89)
        assert s["pnl_unreliable"] is None


def test_unconverted_position_refuses_and_says_it_is_a_unit_mismatch():
    # Same data, but the tracker hasn't been updated so no contract_value is
    # declared: 24.89 base against 2489 "base" is an exact 100x. The panel
    # must refuse — and name the real cause.
    with tempfile.TemporaryDirectory() as d:
        row = _okx_fill(1.0, 1, 0.0, size=24.89)
        row.pop("contract_value")
        a = _adapter(
            Path(d),
            viz={"position": {"net_position_base": 2489.0}},
            fills=[row],
            symbol="SOL-USDT",
            venue="okx",
        )
        s = a.get_stats()
        assert s["pnl_unreliable"] is not None
        assert "factor of 100" in s["pnl_unreliable"]


def test_a_gap_in_the_engines_counter_reaches_the_panel():
    with tempfile.TemporaryDirectory() as d:
        a = _adapter(
            Path(d),
            viz={"position": {"net_position_base": 0.0}},
            fills=[_okx_fill(1.0, 1, 0.0), _okx_fill(2.0, 6, 6.0)],
            symbol="SOL-USDT",
            venue="okx",
        )
        assert "never wrote down" in a.get_stats()["pnl_unreliable"]


def _run_all():
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    _run_all()
