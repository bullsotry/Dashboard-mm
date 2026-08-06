"""Hand-computed cases for inter-venue basis pairing.

Runs under pytest, or standalone: `venv/bin/python tests/test_basis.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.basis import compute_basis_pairs, normalize_base  # noqa: E402


def leg(key, exchange, symbol, mid):
    return {"key": key, "exchange": exchange, "symbol": symbol, "mid": mid}


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


def _run_all():
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    _run_all()
