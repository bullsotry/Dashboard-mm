"""Hand-computed cases for inter-venue basis pairing.

Runs under pytest, or standalone: `venv/bin/python tests/test_basis.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.basis import compute_basis_pairs, normalize_base, split_stale  # noqa: E402


def leg(key, exchange, symbol, mid, ts=0.0):
    return {"key": key, "exchange": exchange, "symbol": symbol, "mid": mid, "ts": ts}


def approx(a, b, tol=1e-9):
    return abs(a - b) <= tol


def test_normalize_base_strips_quote_suffix():
    assert normalize_base("SOLUSDT") == "SOL"
    assert normalize_base("SOL-USD") == "SOL"
    assert normalize_base("BTC_USDC") == "BTC"
    assert normalize_base("ETH/USD") == "ETH"


def test_no_pair_below_two_legs():
    assert compute_basis_pairs([leg("bitunix:SOLUSDT", "bitunix", "SOLUSDT", 100.0)]) == []


def test_same_exchange_never_paired():
    # Two legs, same exchange, same base: a naming collision, not a basis.
    legs = [
        leg("bitunix:SOLUSDT", "bitunix", "SOLUSDT", 100.0),
        leg("bitunix:SOL-PERP", "bitunix", "SOL-PERP", 101.0),
    ]
    assert compute_basis_pairs(legs) == []


def test_cross_venue_basis_bps():
    # Bitunix mid 100.10, Coinbase mid 100.00 -> (100.10-100.00)/100.00*1e4
    # = 10.0 bps, quoted a-relative-to-b where b is the alphabetically-later
    # exchange in the pair ("coinbase" < "bitunix"? no: sorted by exchange
    # name puts "bitunix" before "coinbase", so a=bitunix, b=coinbase).
    legs = [
        leg("coinbase:SOL-USD", "coinbase", "SOL-USD", 100.00),
        leg("bitunix:SOLUSDT", "bitunix", "SOLUSDT", 100.10),
    ]
    pairs = compute_basis_pairs(legs)
    assert len(pairs) == 1
    p = pairs[0]
    assert p["base"] == "SOL"
    assert p["label"] == "bitunix vs coinbase"
    assert approx(p["bps"], 10.0)


def test_zero_mid_legs_excluded():
    legs = [
        leg("bitunix:SOLUSDT", "bitunix", "SOLUSDT", 0.0),
        leg("coinbase:SOL-USD", "coinbase", "SOL-USD", 100.0),
    ]
    assert compute_basis_pairs(legs) == []


def test_unrelated_bases_not_paired():
    legs = [
        leg("bitunix:SOLUSDT", "bitunix", "SOLUSDT", 100.0),
        leg("coinbase:BTC-USD", "coinbase", "BTC-USD", 60000.0),
    ]
    assert compute_basis_pairs(legs) == []


def test_stale_leg_is_dropped_and_named():
    # The live incident this guards against, with its real numbers: a
    # Bitunix leg stamped now, a shadow bot's OKX leg frozen 5 days ago at
    # 72.595 while SOL trades 76.355. Paired, that is
    # (76.355-72.595)/72.595*1e4 = +517.9 bps of fiction.
    now = 1_786_540_400.0
    legs = [
        leg("bitunix:SOLUSDT", "bitunix", "SOLUSDT", 76.355, ts=now - 0.8),
        leg("okx:SOLUSDT", "okx", "SOLUSDT", 72.595, ts=now - 482_459.4),
    ]
    # Unfiltered, the fiction is exactly what comes out — this is the bug.
    assert approx(compute_basis_pairs(legs)[0]["bps"], 517.9, tol=0.05)

    fresh, stale = split_stale(legs, now=now, max_age_s=30.0)
    assert [l["key"] for l in fresh] == ["bitunix:SOLUSDT"]
    assert len(stale) == 1
    assert stale[0]["key"] == "okx:SOLUSDT"
    assert stale[0]["reason"] == "stale mid"
    assert approx(stale[0]["age_s"], 482_459.4, tol=0.01)
    # One fresh leg left -> nothing to pair, so nothing is shown.
    assert compute_basis_pairs(fresh, now=now) == []


def test_leg_without_timestamp_is_refused_not_trusted():
    now = 1_786_540_400.0
    legs = [
        leg("bitunix:SOLUSDT", "bitunix", "SOLUSDT", 76.355, ts=now),
        leg("coinbase:SOL-USD", "coinbase", "SOL-USD", 76.375, ts=0.0),
    ]
    fresh, stale = split_stale(legs, now=now, max_age_s=30.0)
    assert [l["key"] for l in fresh] == ["bitunix:SOLUSDT"]
    assert stale[0]["reason"] == "no timestamp"
    assert stale[0]["age_s"] is None


def test_both_legs_fresh_survive_with_skew():
    # The one real pair on this fleet: Bitunix mid 0.3s old, Coinbase 12s
    # old (measured 2026-08-12). Both are inside the 30s cut, so the pair
    # is priced — but it carries the 11.7s skew it was measured with.
    now = 1_786_540_400.0
    legs = [
        leg("bitunix:SOLUSDT", "bitunix", "SOLUSDT", 76.355, ts=now - 0.3),
        leg("coinbase:SOL-USD", "coinbase", "SOL-USD", 76.375, ts=now - 12.0),
    ]
    fresh, stale = split_stale(legs, now=now, max_age_s=30.0)
    assert stale == []
    pairs = compute_basis_pairs(fresh, now=now)
    assert len(pairs) == 1
    p = pairs[0]
    # (76.355 - 76.375) / 76.375 * 1e4 = -2.6187... bps
    assert approx(p["bps"], -2.61865, tol=1e-4)
    # Tolerance is 1e-6, not 1e-9: these are differences of ~1.79e9-magnitude
    # epoch timestamps, where a double's ulp is already ~2.4e-7.
    assert approx(p["skew_s"], 11.7, tol=1e-6)
    assert approx(p["age_s"], 12.0, tol=1e-6)


def _run_all():
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    _run_all()
