"""Inter-venue basis: the mid-price gap between two legs of the same asset.

Pure, no I/O — takes whatever mids the caller already has in hand (from the
orderbooks it already reads every poll) and pairs them up. This is the one
number generic exchange dashboards don't show and this fleet needs: a cross-
venue MM bot (Bitunix leg quoting, Coinbase leg pricing/hedging, or vice
versa) can drift apart from its own reference without either leg looking
individually wrong.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TypedDict

# Quote suffixes stripped to compare the *base* asset across venues that
# spell the same pair differently: Bitunix's "SOLUSDT" and Coinbase's
# "SOL-USD" must land on the same key ("SOL") or they'd never be paired.
_QUOTE_SUFFIXES = ("USDT", "USDC", "USD")


def normalize_base(symbol: str) -> str:
    s = symbol.upper().replace("-", "").replace("_", "").replace("/", "")
    for suf in _QUOTE_SUFFIXES:
        if s.endswith(suf) and len(s) > len(suf):
            return s[: -len(suf)]
    return s


class Leg(TypedDict):
    key: str
    exchange: str
    symbol: str
    mid: float


class BasisPair(TypedDict):
    base: str
    a: str  # leg key
    b: str  # leg key
    label: str
    bps: float  # (mid_a - mid_b) / mid_b * 10000


def compute_basis_pairs(legs: list[Leg]) -> list[BasisPair]:
    """One entry per pair of legs on *different* exchanges that share a base
    asset and both have a live mid. Two legs on the same exchange are never
    paired — that would be a symbol-naming collision, not a basis."""
    groups: dict[str, list[Leg]] = defaultdict(list)
    for leg in legs:
        if not leg.get("mid") or leg["mid"] <= 0:
            continue
        groups[normalize_base(leg["symbol"])].append(leg)

    pairs: list[BasisPair] = []
    for base, group in groups.items():
        # Stable order so a re-poll with the same legs always emits pairs in
        # the same sequence — the frontend keys history off list position.
        ordered = sorted(group, key=lambda l: (l["exchange"], l["key"]))
        for i in range(len(ordered)):
            for j in range(i + 1, len(ordered)):
                a, b = ordered[i], ordered[j]
                if a["exchange"] == b["exchange"]:
                    continue
                bps = (a["mid"] - b["mid"]) / b["mid"] * 10000.0
                pairs.append(
                    {
                        "base": base,
                        "a": a["key"],
                        "b": b["key"],
                        "label": f"{a['exchange']} vs {b['exchange']}",
                        "bps": bps,
                    }
                )
    return pairs
