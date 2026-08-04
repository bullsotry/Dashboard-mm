"""Hand-computed cases for the FIFO realised-PnL engine.

Runs under pytest, or standalone: `venv/bin/python tests/test_stats.py`.
Every expected value below is worked out by hand in the comment above it —
a PnL test that asserts whatever the code happens to return proves nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.stats import compute_stats  # noqa: E402


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
