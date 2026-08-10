"""Hand-computed cases for the FIFO realised-PnL engine.

Runs under pytest, or standalone: `venv/bin/python tests/test_stats.py`.
Every expected value below is worked out by hand in the comment above it —
a PnL test that asserts whatever the code happens to return proves nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.stats import compute_stats, equity_curve  # noqa: E402


def f(ts, side, price, size, fee=0.0):
    return {"ts": ts, "side": side, "price": price, "size": size, "fee": fee}


def approx(a, b, tol=1e-9):
    return abs(a - b) <= tol


def test_empty_window():
    s = compute_stats([])
    assert s["n_fills"] == 0
    assert s["realised_gross"] == 0.0
    assert s["capture_bps"] is None


def test_long_round_trip():
    # Buy 1 @ 100, sell 1 @ 101 -> (101 - 100) * 1 = +1.00, flat after.
    s = compute_stats([f(1, "buy", 100.0, 1.0), f(2, "sell", 101.0, 1.0)])
    assert approx(s["realised_gross"], 1.0)
    assert approx(s["inventory_base"], 0.0)


def test_short_round_trip():
    # Sell 1 @ 101 first (opens short), buy 1 @ 100 -> (101 - 100) * 1 = +1.00.
    # This is the maker case that a long-only PnL would get backwards.
    s = compute_stats([f(1, "sell", 101.0, 1.0), f(2, "buy", 100.0, 1.0)])
    assert approx(s["realised_gross"], 1.0)
    assert approx(s["inventory_base"], 0.0)


def test_partial_close_leaves_inventory():
    # Buy 2 @ 100, sell 1 @ 101 -> realises on 1 unit only = +1.00,
    # leaving 1 unit long unrealised (deliberately NOT marked to market).
    s = compute_stats([f(1, "buy", 100.0, 2.0), f(2, "sell", 101.0, 1.0)])
    assert approx(s["realised_gross"], 1.0)
    assert approx(s["inventory_base"], 1.0)


def test_fifo_order_matters():
    # Buy 1 @ 100, buy 1 @ 102, sell 1 @ 103.
    # FIFO closes the OLDEST lot (100): (103 - 100) * 1 = +3.00.
    # LIFO would give +1.00, so this asserts the queue discipline itself.
    s = compute_stats(
        [f(1, "buy", 100.0, 1.0), f(2, "buy", 102.0, 1.0), f(3, "sell", 103.0, 1.0)]
    )
    assert approx(s["realised_gross"], 3.0)
    assert approx(s["inventory_base"], 1.0)


def test_flip_through_flat():
    # Buy 1 @ 100, then sell 3 @ 101: closes the 1 long (+1.00) and opens
    # 2 short @ 101. The oversized close must not realise on size it never
    # held -- that would fabricate +3.00 out of a 1-unit position.
    s = compute_stats([f(1, "buy", 100.0, 1.0), f(2, "sell", 101.0, 3.0)])
    assert approx(s["realised_gross"], 1.0)
    assert approx(s["inventory_base"], -2.0)


def test_window_opens_mid_position():
    # Window starts with a sell that has no observed opening lot. It must
    # open a short rather than invent an entry price: realised stays 0.
    s = compute_stats([f(1, "sell", 100.0, 1.0)])
    assert approx(s["realised_gross"], 0.0)
    assert approx(s["inventory_base"], -1.0)


def test_fees_are_subtracted_not_netted_into_gross():
    # Gross +1.00, fees 0.30 -> net +0.70. Gross must stay untouched.
    s = compute_stats(
        [f(1, "buy", 100.0, 1.0, fee=0.1), f(2, "sell", 101.0, 1.0, fee=0.2)]
    )
    assert approx(s["realised_gross"], 1.0)
    assert approx(s["fees"], 0.3)
    assert approx(s["realised_net"], 0.7)


def test_fees_can_flip_a_winning_gross_negative():
    # The whole point of tracking fees for a maker strategy.
    # Gross = (100.01 - 100.00) * 1 = +0.01, fees 0.05 -> net -0.04.
    s = compute_stats(
        [f(1, "buy", 100.0, 1.0, fee=0.025), f(2, "sell", 100.01, 1.0, fee=0.025)]
    )
    assert s["realised_gross"] > 0
    assert s["realised_net"] < 0
    assert approx(s["realised_net"], -0.04)


def test_capture_bps_and_rate():
    # avg buy 100, avg sell 101, mid 100.5 -> 1 / 100.5 * 10000 = 99.50 bps.
    # Span 1..1801 = 1800 s = 0.5 h, 2 fills -> 4 fills/h.
    s = compute_stats([f(1, "buy", 100.0, 1.0), f(1801, "sell", 101.0, 1.0)])
    assert approx(s["capture_bps"], 1.0 / 100.5 * 10000.0, tol=1e-6)
    assert approx(s["span_s"], 1800.0)
    assert approx(s["fills_per_hour"], 4.0, tol=1e-9)


def test_unsorted_input_is_sorted_first():
    # Same fills as the FIFO test, delivered out of order. A tail-read of a
    # jsonl can interleave; the result must not depend on arrival order.
    s = compute_stats(
        [f(3, "sell", 103.0, 1.0), f(1, "buy", 100.0, 1.0), f(2, "buy", 102.0, 1.0)]
    )
    assert approx(s["realised_gross"], 3.0)


def test_garbage_rows_are_skipped():
    # Zero size, zero price, unknown side: dropped, not crashed on.
    s = compute_stats(
        [
            f(1, "buy", 100.0, 0.0),
            f(2, "", 100.0, 1.0),
            f(3, "sell", 0.0, 1.0),
            f(4, "buy", 100.0, 1.0),
        ]
    )
    assert s["n_fills"] == 1
    assert approx(s["inventory_base"], 1.0)


def test_reliable_when_replay_matches_the_exchange():
    # Buy 2, sell 1 -> replay holds +1, exchange agrees. Nothing to warn about.
    s = compute_stats(
        [f(1, "buy", 100.0, 2.0), f(2, "sell", 101.0, 1.0)], net_position_base=1.0
    )
    assert s["pnl_unreliable"] is None


def test_flags_an_incomplete_ledger():
    # Replay lands on +1 while the exchange holds -5.78: history is missing,
    # so every close matched lots the replay invented. This is the live case
    # that produced a confident, wrong PnL.
    s = compute_stats(
        [f(1, "buy", 100.0, 2.0), f(2, "sell", 101.0, 1.0)], net_position_base=-5.78
    )
    assert s["pnl_unreliable"] is not None
    assert "ledger incomplete" in s["pnl_unreliable"]


def test_small_gap_is_rounding_not_a_missing_history():
    s = compute_stats(
        [f(1, "buy", 100.0, 2.0), f(2, "sell", 101.0, 1.0)], net_position_base=1.01
    )
    assert s["pnl_unreliable"] is None


def test_flags_hedge_mode_even_when_inventory_agrees():
    # Hedge mode breaks FIFO regardless of whether the net happens to line
    # up, because netting two independent books is the wrong model entirely.
    s = compute_stats(
        [f(1, "buy", 100.0, 2.0), f(2, "sell", 101.0, 1.0)],
        net_position_base=1.0,
        hedge_mode=True,
    )
    assert s["pnl_unreliable"] is not None
    assert "hedge" in s["pnl_unreliable"]


def test_unknown_position_is_not_an_accusation():
    # No position published: we cannot check the replay, but we also have no
    # evidence against it. Silence, not a warning.
    s = compute_stats([f(1, "buy", 100.0, 2.0)], net_position_base=None)
    assert s["pnl_unreliable"] is None


def test_equity_curve_matches_compute_stats_final_point():
    # Buy 1 @ 100 (fee 0.1), sell 1 @ 101 (fee 0.1) -> realised 1.0, net 0.8.
    fills = [f(1, "buy", 100.0, 1.0, fee=0.1), f(2, "sell", 101.0, 1.0, fee=0.1)]
    stats = compute_stats(fills)
    curve = equity_curve(fills)
    assert len(curve) == 2
    # First point: net -0.1, never above 0 (the curve's implicit start) yet,
    # so peak stays 0 and drawdown is the full -0.1 -> 0.1.
    assert curve[0] == {
        "ts": 1,
        "realised_net": -0.1,
        "inventory_base": 1.0,
        "cum_volume_quote": 100.0,
        "drawdown": 0.1,
        "max_drawdown": 0.1,
        "pnl_unreliable": None,
    }
    assert approx(curve[-1]["realised_net"], stats["realised_net"])
    assert approx(curve[-1]["inventory_base"], stats["inventory_base"])
    assert approx(curve[-1]["cum_volume_quote"], stats["volume_quote"])


def test_equity_curve_volume_is_a_running_sum_of_notional():
    # 100*1 + 101*1 = 100, then 201 cumulative -- never resets, never nets
    # buys against sells the way realised PnL does.
    fills = [f(1, "buy", 100.0, 1.0), f(2, "sell", 101.0, 1.0), f(3, "buy", 50.0, 2.0)]
    curve = equity_curve(fills)
    assert approx(curve[0]["cum_volume_quote"], 100.0)
    assert approx(curve[1]["cum_volume_quote"], 201.0)
    assert approx(curve[2]["cum_volume_quote"], 301.0)


def test_equity_curve_volume_stays_defendable_when_pnl_is_not():
    # Same hedge-mode case that flags realised PnL as unreliable below --
    # the volume figure must not be blanked or zeroed along with it, because
    # it doesn't depend on the FIFO replay that hedge mode breaks.
    fills = [f(1, "buy", 100.0, 2.0), f(2, "sell", 101.0, 1.0)]
    curve = equity_curve(fills, net_position_base=1.0, hedge_mode=True)
    assert curve[-1]["pnl_unreliable"] is not None
    assert approx(curve[-1]["cum_volume_quote"], 200.0 + 101.0)


def test_equity_curve_is_monotonic_in_time_and_cumulative():
    fills = [f(3, "buy", 100.0, 1.0), f(1, "buy", 99.0, 1.0), f(2, "sell", 101.0, 1.0)]
    curve = equity_curve(fills)  # ordered by ts regardless of input order
    assert [p["ts"] for p in curve] == [1, 2, 3]
    # buy@99 -> flat until sell@101 closes it (+2.0) -> buy@100 opens fresh
    assert approx(curve[1]["realised_net"], 2.0)
    assert approx(curve[2]["realised_net"], 2.0)  # unrealised open lot doesn't move it
    assert curve[2]["inventory_base"] == 1.0


def test_equity_curve_carries_the_same_unreliable_verdict_as_compute_stats():
    fills = [f(1, "buy", 100.0, 2.0), f(2, "sell", 101.0, 1.0)]
    stats = compute_stats(fills, net_position_base=1.0, hedge_mode=True)
    curve = equity_curve(fills, net_position_base=1.0, hedge_mode=True)
    assert curve[-1]["pnl_unreliable"] == stats["pnl_unreliable"]
    assert all(p["pnl_unreliable"] == stats["pnl_unreliable"] for p in curve)


def test_equity_curve_empty_fills():
    assert equity_curve([]) == []


def test_equity_curve_drawdown_tracks_peak_to_trough():
    # buy@100 (net 0) -> sell@103 (+3, net 3, new peak) -> sell@100 opens a
    # short (net 3, unchanged) -> buy@110 closes it at a 10 loss (net -7,
    # 10 below the peak of 3) -> buy@90 (net -7, unchanged) -> sell@95
    # closes it at +5 (net -2, still 5 below the peak of 3).
    fills = [
        f(1, "buy", 100.0, 1.0),
        f(2, "sell", 103.0, 1.0),
        f(3, "sell", 100.0, 1.0),
        f(4, "buy", 110.0, 1.0),
        f(5, "buy", 90.0, 1.0),
        f(6, "sell", 95.0, 1.0),
    ]
    curve = equity_curve(fills)
    net = [round(p["realised_net"], 6) for p in curve]
    assert net == [0.0, 3.0, 3.0, -7.0, -7.0, -2.0]

    dd = [round(p["drawdown"], 6) for p in curve]
    assert dd == [0.0, 0.0, 0.0, 10.0, 10.0, 5.0]

    max_dd = [round(p["max_drawdown"], 6) for p in curve]
    assert max_dd == [0.0, 0.0, 0.0, 10.0, 10.0, 10.0]  # monotonically non-decreasing
    assert max_dd[-1] == 10.0  # the window's overall max drawdown


def test_equity_curve_max_drawdown_zero_when_curve_never_dips_below_its_peak():
    fills = [f(1, "buy", 100.0, 1.0), f(2, "sell", 101.0, 1.0)]  # only ever climbs
    curve = equity_curve(fills)
    assert all(approx(p["max_drawdown"], 0.0) for p in curve)


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  ok   {name}")
        except AssertionError:
            failures += 1
            print(f"  FAIL {name}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
