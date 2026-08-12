"""Hand-computed cases for post-fill markout.

Runs under pytest, or standalone: `venv/bin/python tests/test_markouts.py`.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.markouts import aggregate, markouts_path_for  # noqa: E402


def approx(a, b, tol=1e-9):
    return abs(a - b) <= tol


def row(side, fill_mid, **mids):
    r = {"side": side, "fill_mid": fill_mid, "fill_price": fill_mid, "venue": "bitunix"}
    r.update(mids)
    return r


def test_buy_that_the_market_moves_toward_is_positive():
    # Bought with the mid at 100.00; 5s later it is 100.05.
    # (100.05 - 100.00) / 100.00 * 1e4 = +5.0 bps in the maker's favour.
    pts = {p["horizon"]: p for p in aggregate([row("buy", 100.0, mid_5s=100.05)])}
    assert approx(pts["5s"]["bps"], 5.0)
    assert pts["5s"]["n"] == 1


def test_sell_is_signed_the_other_way():
    # Sold at a mid of 100.00; 5s later it is 99.95. The mid fell after a
    # sell, which is favourable: +5.0 bps, not -5.0. Getting this sign
    # backwards would invert the entire panel's meaning.
    pts = {p["horizon"]: p for p in aggregate([row("sell", 100.0, mid_5s=99.95)])}
    assert approx(pts["5s"]["bps"], 5.0)


def test_adverse_selection_reads_negative():
    # The failure this panel exists to reveal: bought, and the mid walked
    # away downwards. (99.98 - 100.00)/100 * 1e4 = -2.0 bps.
    pts = {p["horizon"]: p for p in aggregate([row("buy", 100.0, mid_10s=99.98)])}
    assert approx(pts["10s"]["bps"], -2.0)


def test_sides_are_reported_separately():
    # A book run over on one side only: buys -4 bps, sells +2 bps, mean -1.
    # A blended average alone would hide which side is bleeding.
    rows = [
        row("buy", 100.0, mid_1s=99.96),
        row("sell", 100.0, mid_1s=99.98),
    ]
    pts = {p["horizon"]: p for p in aggregate(rows)}
    assert approx(pts["1s"]["buy_bps"], -4.0)
    assert approx(pts["1s"]["sell_bps"], 2.0)
    assert approx(pts["1s"]["bps"], -1.0)


def test_side_casing_does_not_flip_the_sign():
    """Bitunix writes BUY/SELL, Coinbase writes buy/sell — in the same file.

    An analysis script that compared against "buy" without normalising read
    every Bitunix buy as a sell and inverted its sign, turning a real
    -0.38bps of adverse selection into a reassuring +0.11. Both spellings
    must produce the same number.
    """
    lower = aggregate([row("buy", 100.0, mid_1s=100.05)])[1]
    upper = aggregate([{**row("buy", 100.0, mid_1s=100.05), "side": "BUY"}])[1]
    assert approx(lower["bps"], 5.0)
    assert approx(upper["bps"], 5.0)
    assert approx(aggregate([{**row("sell", 100.0, mid_1s=99.95), "side": "SELL"}])[1]["bps"], 5.0)


def test_missing_horizon_is_none_not_zero():
    # "The mid didn't move" and "we never sampled the mid" must not render
    # identically — only one of them is good news.
    pts = {p["horizon"]: p for p in aggregate([row("buy", 100.0, mid_1s=100.0)])}
    assert approx(pts["1s"]["bps"], 0.0)
    assert pts["30s"]["bps"] is None
    assert pts["30s"]["n"] == 0


def test_rows_without_a_usable_mid_are_skipped():
    rows = [
        row("buy", 0.0, mid_1s=100.0),  # no fill mid
        row("buy", 100.0, mid_1s=None),  # horizon not sampled
        {"side": "sideways", "fill_mid": 100.0, "mid_1s": 101.0},  # unknown side
        row("buy", 100.0, mid_1s=100.01),
    ]
    pts = {p["horizon"]: p for p in aggregate(rows)}
    assert pts["1s"]["n"] == 1
    assert approx(pts["1s"]["bps"], 1.0)


def test_all_horizons_are_always_present_and_ordered():
    pts = aggregate([])
    assert [p["horizon"] for p in pts] == ["100ms", "1s", "5s", "10s", "30s"]
    assert all(p["bps"] is None and p["n"] == 0 for p in pts)


def test_markout_log_located_next_to_its_own_ledger():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        (tmp / "markouts.jsonl").write_text("")
        (tmp / "markouts_okx.jsonl").write_text("")
        assert markouts_path_for(tmp / "fills.jsonl").name == "markouts.jsonl"
        assert markouts_path_for(tmp / "fills_okx.jsonl").name == "markouts_okx.jsonl"
        assert markouts_path_for(tmp / "fills_other.jsonl") is None
        assert markouts_path_for(tmp / "sessions_all.csv") is None


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
