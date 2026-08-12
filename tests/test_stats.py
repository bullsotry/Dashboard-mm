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


def test_unknown_position_is_not_an_accusation_but_is_not_a_clean_bill_either():
    # No position published: we cannot check the replay, but we also have no
    # evidence against it. The figure is not withheld (`pnl_unreliable`
    # stays None) — but it is not silently blessed either, which is how the
    # Coinbase leg came to be the one number on screen shown as trustworthy
    # purely because it was the one that could not be tested.
    s = compute_stats([f(1, "buy", 100.0, 2.0)], net_position_base=None)
    assert s["pnl_unreliable"] is None
    assert s["pnl_unverified"] is not None
    assert "publishes no position" in s["pnl_unverified"]


def test_a_checked_replay_is_not_marked_unverified():
    s = compute_stats([f(1, "buy", 100.0, 2.0)], net_position_base=2.0)
    assert s["pnl_unreliable"] is None
    assert s["pnl_unverified"] is None


def test_hedge_mode_is_refused_not_merely_unverified():
    # Hedge mode is positive evidence that FIFO is the wrong model, so it
    # withholds rather than annotates.
    s = compute_stats([f(1, "buy", 100.0, 2.0)], net_position_base=None, hedge_mode=True)
    assert s["pnl_unreliable"] is not None
    assert s["pnl_unverified"] is None


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


# --- Session windows (since_ts) -------------------------------------------
#
# The case that matters: a bot restarts holding inventory it bought during
# the *previous* session, and sells it during this one. Whether the pre-roll
# happens decides whether the session's PnL is real or invented.


def test_session_opening_mid_position_prices_against_real_lots():
    # Previous session: buy 2 @ 100 (cost 200).
    # This session (from ts=10): sell 2 @ 110 -> (110-100)*2 = +20.00 gross.
    # The lots were bought before the window, so only a replay that walks
    # them can price this exit at all.
    fills = [f(1, "buy", 100.0, 2.0), f(11, "sell", 110.0, 2.0)]
    s = compute_stats(fills, since_ts=10.0)
    assert approx(s["realised_gross"], 20.0)
    assert s["n_fills"] == 1  # only the sell is *counted*
    assert s["n_preroll_fills"] == 1  # but the buy was replayed
    assert approx(s["opening_inventory_base"], 2.0)
    assert approx(s["inventory_base"], 0.0)  # flat at the end of the session
    assert approx(s["volume_quote"], 220.0)  # 110 * 2, the session's own only


def test_session_without_preroll_would_have_invented_the_exit():
    # The same session, with the previous session's buy genuinely absent
    # from the ledger: the sell has nothing to close, so it opens a short
    # and realises nothing. This is what a naive `ts >= start` filter would
    # produce, and it is why the pre-roll exists — the figure is not just
    # different, it is a different position (-2 base) than the bot holds.
    s = compute_stats([f(11, "sell", 110.0, 2.0)], since_ts=10.0)
    assert approx(s["realised_gross"], 0.0)
    assert approx(s["inventory_base"], -2.0)


def test_session_opening_inventory_mismatch_refuses_the_pnl():
    # Replay rebuilds +2.00 of opening inventory, but the bot recorded that
    # it was holding +7.00 when the session's first fill landed: the ledger
    # doesn't reach back far enough, so the lots this session closes against
    # are partly invented. Refuse, and say which two numbers disagree.
    fills = [f(1, "buy", 100.0, 2.0), f(11, "sell", 110.0, 2.0)]
    s = compute_stats(fills, since_ts=10.0, opening_inventory_recorded=7.0)
    assert s["pnl_unreliable"] is not None
    assert "+2.00" in s["pnl_unreliable"] and "+7.00" in s["pnl_unreliable"]

    # Matching within tolerance: nothing to complain about.
    ok = compute_stats(fills, since_ts=10.0, opening_inventory_recorded=2.0)
    assert ok["pnl_unreliable"] is None


def test_session_curve_starts_at_zero_not_at_the_previous_session_total():
    # Previous session made +5 (buy 1 @ 100, sell 1 @ 105) and left the bot
    # holding 1 @ 100. This session sells it @ 110 -> +10 for the session.
    # The curve's only point must read +10, not +15: a session that inherits
    # the last one's total is not a session.
    fills = [
        f(1, "buy", 100.0, 1.0),
        f(2, "sell", 105.0, 1.0),
        f(3, "buy", 100.0, 1.0),
        f(11, "sell", 110.0, 1.0),
    ]
    curve = equity_curve(fills, since_ts=10.0)
    assert len(curve) == 1
    assert approx(curve[0]["realised_net"], 10.0)
    assert approx(curve[0]["inventory_base"], 0.0)


def test_session_span_measured_from_session_start_not_first_fill():
    # Session starts at ts=0, first fill only lands at ts=1800, last at
    # ts=3600. The bot was running for the whole hour: span 3600s, and
    # 2 fills/hour — not 2 fills over the 1800s between them (4/h), which
    # would flatter the rate by 2x.
    fills = [f(1800, "buy", 100.0, 1.0), f(3600, "sell", 101.0, 1.0)]
    s = compute_stats(fills, since_ts=0.0)
    assert approx(s["span_s"], 3600.0)
    assert approx(s["fills_per_hour"], 2.0)


# --- Cash-flow PnL --------------------------------------------------------
#
# The figure that survives hedge mode, because it matches no lots.


def fm(ts, side, price, size, fee=0.0, mid=None, inv_before=None):
    return {
        "ts": ts, "side": side, "price": price, "size": size, "fee": fee,
        "mid_at_fill": mid if mid is not None else price,
        "inventory_before": inv_before,
    }


def test_cash_pnl_matches_fifo_on_a_flat_round_trip():
    # Buy 1 @ 100 then sell 1 @ 101, fees 0.10 total, ending flat.
    #   cash in 101, cash out 100, closing inventory 0, opening 0
    #   -> 101 - 100 + 0 - 0 - 0.10 = +0.90
    # Which is exactly realised_net, because nothing is left marked.
    fills = [
        fm(1, "buy", 100.0, 1.0, fee=0.05, inv_before=0.0),
        fm(2, "sell", 101.0, 1.0, fee=0.05),
    ]
    s = compute_stats(fills, since_ts=0.0)
    assert approx(s["cash_pnl"], 0.90)
    assert approx(s["realised_net"], 0.90)
    assert s["cash_pnl_basis"] == "ledger"


def test_cash_pnl_marks_the_inventory_the_session_still_carries():
    # Buy 2 @ 100, sell 1 @ 101, ending long 1. Last mid is 102.
    #   cash in 101, cash out 200, closing inv 1 @ 102, opening 0, no fees
    #   -> 101 - 200 + 102 - 0 = +3.00
    # FIFO realises only the closed unit (+1.00); the other +2.00 is the
    # open unit marked to market. The two figures differ by exactly the
    # exposure, which is the point of showing both.
    fills = [
        fm(1, "buy", 100.0, 2.0, inv_before=0.0),
        fm(2, "sell", 101.0, 1.0, mid=102.0),
    ]
    s = compute_stats(fills, since_ts=0.0)
    assert approx(s["cash_pnl"], 3.0)
    assert approx(s["realised_gross"], 1.0)


def test_cash_pnl_survives_hedge_mode_where_fifo_refuses():
    # The real case: a bot holding both sides. FIFO must refuse; the
    # cash-flow figure is unaffected because it never asks which book moved.
    #   sell 1 @ 101, buy 1 @ 100, opening inv 0, closing 0, fees 0
    #   -> 101 - 100 = +1.00
    fills = [
        fm(1, "sell", 101.0, 1.0, inv_before=0.0),
        fm(2, "buy", 100.0, 1.0),
    ]
    s = compute_stats(fills, since_ts=0.0, hedge_mode=True)
    assert s["pnl_unreliable"] is not None  # realised_net stays refused
    assert approx(s["cash_pnl"], 1.0)  # this one still answers


def test_cash_pnl_uses_the_ledgers_opening_inventory_not_the_replays():
    # Session opens holding 2 (the bot says so on its first fill), sells
    # them at 110 having marked in at 100:
    #   cash in 220, cash out 0, closing inv 0, opening 2 @ 100
    #   -> 220 + 0 - 200 = +20.00
    # No pre-roll at all is available here, so the replay would have said
    # the session opened flat and claimed +220. The ledger's own reading is
    # what keeps it honest.
    s = compute_stats([fm(11, "sell", 110.0, 2.0, mid=100.0, inv_before=2.0)], since_ts=10.0)
    assert s["cash_pnl_basis"] == "ledger"
    assert approx(s["cash_pnl"], 20.0)


def test_cash_pnl_falls_back_to_replay_and_says_so():
    # Coinbase-shaped rows: no inventory recorded anywhere. The replay's
    # rebuilt opening inventory is used instead, and the basis names it so
    # the reader knows which guarantee applies.
    fills = [fm(1, "buy", 100.0, 1.0), fm(11, "sell", 110.0, 1.0)]
    s = compute_stats(fills, since_ts=10.0)
    assert s["cash_pnl_basis"] == "replay"
    # opening inv 1 @ mark 110 (the only mark this session has), sells it at
    # 110 -> 110 + 0 - 110 = 0.00. Marked in and out at the same price
    # because the session saw only one fill; the FIFO figure (+10) is the
    # one that knows what was paid for the lot.
    assert approx(s["cash_pnl"], 0.0)
    assert approx(s["realised_gross"], 10.0)


def test_cash_pnl_refuses_a_ledger_counting_contracts():
    # The real OKX shape: `size` is in base (0.2 SOL) but `inventory_before`
    # counts contracts (1 contract = 0.01 SOL), so the recorded inventory
    # climbs by 20 while the fills only bought 0.2. Marking the recorded
    # figure would value a 0.4-SOL book as a 40-SOL one — on the live
    # window that produced a -24.57 session PnL on 777 USD of volume.
    fills = [
        fm(1, "buy", 75.0, 0.2, mid=75.0, inv_before=966.0),
        fm(2, "buy", 75.0, 0.2, mid=75.0, inv_before=986.0),
    ]
    s = compute_stats(fills, since_ts=0.0)
    assert s["cash_pnl"] is None
    assert "not in the same units" in s["cash_pnl_unavailable"]


def test_cash_pnl_tolerates_inventory_readings_that_lag_their_fills():
    # The Bitunix shape: the recorded inventory is sampled asynchronously
    # and barely moves across a window whose fills net out to +2 base. That
    # is lag, not a units bug — the opening reading is still in base units
    # and still the best anchor available. Rejecting it here threw away
    # three sessions that matched the account's own equity delta to within
    # 0.15 USD.
    fills = [
        fm(1, "buy", 100.0, 1.0, mid=100.0, inv_before=5.0),
        fm(2, "buy", 100.0, 1.0, mid=100.0, inv_before=5.0),
    ]
    s = compute_stats(fills, since_ts=0.0)
    assert s["cash_pnl_unavailable"] is None
    assert s["cash_pnl_basis"] == "ledger"


def test_cash_pnl_accepts_a_ledger_that_agrees_with_its_fills():
    # Same shape, consistent units: two buys of 0.2 move the recorded
    # inventory from 1.00 to 1.20. Nothing to refuse.
    #   cash out 30, closing inv 1.4 @ 75, opening 1.0 @ 75 -> -30 + 105 - 75 = 0
    fills = [
        fm(1, "buy", 75.0, 0.2, mid=75.0, inv_before=1.0),
        fm(2, "buy", 75.0, 0.2, mid=75.0, inv_before=1.2),
    ]
    s = compute_stats(fills, since_ts=0.0)
    assert s["cash_pnl_unavailable"] is None
    assert s["cash_pnl_basis"] == "ledger"
    assert approx(s["cash_pnl"], 0.0)




# --- Curve decimation (server-side transport thinning) --------------------
# Lives here rather than in a server test because what it must protect is a
# property of the curve, not of the route.


def test_decimation_preserves_the_envelope_and_the_last_point():
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
    from server import _decimate_curve

    # A curve that climbs to +10, plunges to -40 in a single point, then
    # recovers. Every-Nth sampling would very likely drop the -40 — which is
    # the entire drawdown. The bucket min/max rule cannot.
    curve = [
        {"ts": i, "realised_net": float(i % 10), "inventory_base": 0.0,
         "cum_volume_quote": float(i), "drawdown": 0.0, "max_drawdown": 40.0}
        for i in range(4000)
    ]
    curve[1234]["realised_net"] = -40.0
    curve[3777]["realised_net"] = 99.0

    out = _decimate_curve(curve, 600)
    assert len(out) <= 601
    values = [p["realised_net"] for p in out]
    assert -40.0 in values, "the trough must survive thinning"
    assert 99.0 in values, "the peak must survive thinning"
    assert out[-1] is curve[-1], "the live value must be the real last point"
    # Time order is what the chart draws along; thinning must not scramble it.
    assert all(a["ts"] <= b["ts"] for a, b in zip(out, out[1:]))


def test_decimation_leaves_a_short_curve_untouched():
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
    from server import _decimate_curve

    curve = [{"ts": i, "realised_net": float(i)} for i in range(120)]
    assert _decimate_curve(curve, 600) is curve


def test_cash_pnl_anchors_on_the_exchange_position_not_the_ledger():
    """The live 2026-08-12 Bitunix case, scaled down.

    The ledger claimed the session opened flat (`inventory_before` 0.00);
    it had actually opened 2.86 base short, and that field was separately
    observed frozen at 0.00 across three earlier sessions. Anchoring on it
    valued inventory the bot did not hold.

    Here: fills net +3 over the session, the exchange reports +1, so the
    session must have opened at -2 — whatever the ledger says.
      cash in 100, cash out 400, close +1 @ 100, open -2 @ 100
      -> 100 - 400 + 100 - (-200) = 0.00
    """
    fills = [
        fm(1, "buy", 100.0, 4.0, mid=100.0, inv_before=0.0),
        fm(2, "sell", 100.0, 1.0, mid=100.0),
    ]
    s = compute_stats(fills, since_ts=0.0, net_position_base=1.0)
    assert s["cash_pnl_basis"] == "exchange position"
    assert approx(s["opening_inventory_base"], 0.0)  # replay's own view, unused
    assert approx(s["cash_pnl"], 0.0)


def test_anchoring_keeps_a_missing_fill_from_costing_its_full_notional():
    """Why anchoring beats refusing outright.

    A sell of 1 @ 100 never reaches the ledger. Unanchored, the closing
    inventory would be overstated by 1 and marked at ~100, throwing the
    figure off by ~100. Anchored, the uncounted 100 of cash and the
    correspondingly shifted opening inventory cancel almost exactly: what
    survives is only the gap between the missing fill's price and the mark.
    """
    complete = [
        fm(1, "buy", 100.0, 2.0, mid=100.0),
        fm(2, "sell", 101.0, 1.0, mid=100.5),
        fm(3, "sell", 100.0, 1.0, mid=100.0),
    ]
    full = compute_stats(complete, since_ts=0.0, net_position_base=0.0)
    # Same session, middle fill lost. The exchange still reports flat.
    lossy = [complete[0], complete[2]]
    partial = compute_stats(lossy, since_ts=0.0, net_position_base=0.0)
    drift = abs(partial["cash_pnl"] - full["cash_pnl"])
    # The residual is exactly qty * (missing fill's price - opening mark)
    # = 1 * (101 - 100) = 1.00, against a notional of 101 that an unanchored
    # calculation would have swallowed whole.
    assert approx(drift, 1.0)
    assert drift < 0.05 * 101.0


def test_finished_session_anchors_by_walking_back_from_todays_position():
    # Session ran to ts=5 and netted +3; another +2 was bought since. The
    # exchange holds +6 now, so the session closed at +4 and opened at +1.
    #   cash in 0, cash out 300, close +4 @ 100, open +1 @ 100
    #   -> -300 + 400 - 100 = 0.00
    fills = [
        fm(1, "buy", 100.0, 3.0, mid=100.0),
        fm(9, "buy", 100.0, 2.0, mid=100.0),  # after the session ended
    ]
    s = compute_stats(fills, since_ts=0.0, until_ts=5.0, net_position_base=6.0)
    assert s["cash_pnl_basis"] == "exchange position"
    assert approx(s["cash_pnl"], 0.0)


def test_finished_session_is_not_checked_against_todays_position():
    # A session that ended days ago has nothing to do with the position held
    # now, so the closing check must not fire on it.
    fills = [
        fm(1, "buy", 100.0, 4.0, mid=100.0, inv_before=0.0),
        fm(2, "sell", 100.0, 1.0, mid=100.0),
    ]
    s = compute_stats(fills, since_ts=0.0, until_ts=5.0, net_position_base=999.0)
    assert s["cash_pnl"] is not None


def test_anchor_is_refused_when_the_position_is_in_contract_units():
    # OKX again: `size` is base, but the position and inventory fields count
    # contracts. Anchoring on that position would mark 100x the real book —
    # live, it produced -76.60 USD on a 777 USD session.
    fills = [
        fm(1, "buy", 75.0, 0.2, mid=75.0, inv_before=966.0),
        fm(2, "buy", 75.0, 0.2, mid=75.0, inv_before=986.0),
    ]
    s = compute_stats(fills, since_ts=0.0, net_position_base=1026.0)
    assert s["cash_pnl"] is None
    assert "not in the same units" in s["cash_pnl_unavailable"]


# --------------------------------------------------------------------------
# fill_seq: the ledger proving its own completeness (adapters/stats.py:_sequence_gap)
# --------------------------------------------------------------------------


def fs(ts, side, price, size, seq, run=1, fee=0.0):
    return {
        "ts": ts, "side": side, "price": price, "size": size, "fee": fee,
        "fill_seq": seq, "fill_run_id": run,
    }


def test_sequence_gap_refuses_and_counts_the_missing_fills():
    # Counter goes 1, 2, 5: fills 3 and 4 were traded and never written
    # down, so two are missing. The arithmetic below is unaffected by their
    # absence — which is exactly the problem: it looks fine and isn't.
    fills = [
        fs(1, "buy", 100.0, 1.0, 1),
        fs(2, "sell", 101.0, 1.0, 2),
        fs(3, "buy", 100.0, 1.0, 5),
    ]
    s = compute_stats(fills)
    assert s["pnl_unreliable"] is not None
    assert "2 fill(s)" in s["pnl_unreliable"]
    assert "2 -> 5" in s["pnl_unreliable"]


def test_a_complete_sequence_is_not_refused():
    fills = [fs(1, "buy", 100.0, 1.0, 7), fs(2, "sell", 101.0, 1.0, 8)]
    s = compute_stats(fills)
    assert s["pnl_unreliable"] is None


def test_a_restart_is_not_a_gap():
    # New process: fill_run_id changes and the counter restarts at 1. That
    # is a restart, not a missing fill, and must not refuse the window.
    fills = [
        fs(1, "buy", 100.0, 1.0, 812, run=1),
        fs(2, "sell", 101.0, 1.0, 1, run=2),
        fs(3, "buy", 100.0, 1.0, 2, run=2),
    ]
    s = compute_stats(fills)
    assert s["pnl_unreliable"] is None


def test_rows_without_a_counter_claim_nothing():
    # Pre-patch rows carry no fill_seq. Absence of evidence is not evidence
    # of a hole: the window must not be refused on their account.
    fills = [f(1, "buy", 100.0, 1.0), f(2, "sell", 101.0, 1.0)]
    s = compute_stats(fills)
    assert s["pnl_unreliable"] is None


def test_a_gap_outside_the_window_does_not_refuse_the_session():
    # The hole is between fills 1 and 2, both before the session opens at
    # t=10. Tonight's session is measured on rows 3 and 4, which are
    # contiguous — the damage the old hole did to the opening lots is
    # `_opening_gap`'s job, not this check's.
    fills = [
        fs(1, "buy", 100.0, 1.0, 1),
        fs(2, "sell", 101.0, 1.0, 9),
        fs(11, "buy", 100.0, 1.0, 10),
        fs(12, "sell", 101.0, 1.0, 11),
    ]
    s = compute_stats(fills, since_ts=10.0)
    assert s["pnl_unreliable"] is None


def test_sequence_gap_also_blanks_the_curve():
    fills = [
        fs(1, "buy", 100.0, 1.0, 1),
        fs(2, "sell", 101.0, 1.0, 4),
    ]
    curve = equity_curve(fills)
    assert curve
    assert all("2 fill(s)" in p["pnl_unreliable"] for p in curve)


def test_sequence_gap_is_named_before_the_inventory_mismatch():
    # Both fire: the counter jumped *and* the replay disagrees with the
    # exchange. The gap is the cause, the mismatch its symptom.
    fills = [fs(1, "buy", 100.0, 1.0, 1), fs(2, "buy", 100.0, 1.0, 4)]
    s = compute_stats(fills, net_position_base=50.0)
    assert "never wrote down" in s["pnl_unreliable"]


def test_unit_mismatch_is_named_instead_of_blamed_on_the_ledger():
    # Replay holds 2.00 base, the "exchange" reports 200 — an exact 100x,
    # i.e. contracts against base, not a lost history.
    s = compute_stats([f(1, "buy", 100.0, 2.0)], net_position_base=200.0)
    assert "factor of 100" in s["pnl_unreliable"]


def test_an_ordinary_mismatch_gets_no_unit_hint():
    # 2.00 vs 7.31 is what a genuinely incomplete ledger looks like.
    s = compute_stats([f(1, "buy", 100.0, 2.0)], net_position_base=7.31)
    assert "factor of" not in s["pnl_unreliable"]




def test_a_non_round_ratio_is_not_called_a_unit_mismatch():
    # The live OKX window: replay +43.54 base against a position reported as
    # 4854 — a ratio of 111.5. Contract values are powers of ten, so this is
    # a unit bug *and* a real drift on top; naming a "factor of 111" would
    # be a number that is neither exact nor a unit.
    s = compute_stats([f(1, "buy", 100.0, 43.54)], net_position_base=4854.0)
    assert "factor of" not in s["pnl_unreliable"]
    assert "ledger incomplete" in s["pnl_unreliable"]


def test_a_thousandfold_ratio_still_earns_the_hint():
    s = compute_stats([f(1, "buy", 100.0, 2.0)], net_position_base=2000.0)
    assert "factor of 1000" in s["pnl_unreliable"]


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
