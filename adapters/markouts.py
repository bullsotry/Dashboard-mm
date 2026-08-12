"""Markout: where the mid went after each fill.

The one measurement that separates market making from being picked off. A
maker earns the spread at the moment of the fill and then *keeps* it only if
the mid doesn't walk away afterwards. Capture measured at t=0 says nothing
about that; a +2bps edge given back by -2.5bps of adverse selection is a
machine for paying fees, and it looks identical to a profitable one on every
panel this dashboard had before.

The fleet's tracker already records this (`markouts.jsonl` next to the fill
ledger), sampling the mid at 100ms/1s/5s/10s/30s after each fill. Nothing
read it until now.

WHAT IS MEASURED, AND WHY THIS SHAPE AND NOT ANOTHER
----------------------------------------------------
Per fill, signed so that positive always means "the market moved my way":

    buy :  (mid_at_horizon - mid_at_fill) / mid_at_fill * 10000
    sell:  (mid_at_fill - mid_at_horizon) / mid_at_fill * 10000

This is a *relative* figure — both terms share `fill_mid` — which is what
makes it trustworthy here. The absolute edge (fill price against the mid) is
NOT computed, deliberately: `fill_mid` is captured when the fill is
processed, i.e. after execution, so a passive maker's own fill removes its
level and the mid recoils against it. Measured on this fleet 2026-08-12,
that made 50% of Bitunix fills and 59% of Coinbase fills look like they
executed on the wrong side of the mid — an artefact of when the mid is
sampled, not a description of the strategy. Any absolute edge built on this
field would inherit that bias, so this module refuses to produce one and
reports only differences in which the bias cancels.

Buy and sell are reported separately as well as together, because a book
that gets run over on one side only is a real and common failure that a
blended average hides.
"""

from __future__ import annotations

import statistics
from pathlib import Path
from typing import Iterable, TypedDict

# The horizons the tracker samples, in the order they must be displayed —
# a markout profile is read left to right as time since the fill.
HORIZONS: tuple[tuple[str, str], ...] = (
    ("mid_100ms", "100ms"),
    ("mid_1s", "1s"),
    ("mid_5s", "5s"),
    ("mid_10s", "10s"),
    ("mid_30s", "30s"),
)


class MarkoutPoint(TypedDict):
    horizon: str
    bps: float | None  # mean, all fills
    median_bps: float | None
    buy_bps: float | None
    sell_bps: float | None
    n: int


def markouts_path_for(fills_path: Path) -> Path | None:
    """The markout log that goes with a fill ledger, by the tracker's own
    naming convention in that directory:

        fills.jsonl      -> markouts.jsonl
        fills_okx.jsonl  -> markouts_okx.jsonl
    """
    stem = fills_path.name
    if not stem.startswith("fills") or not stem.endswith(".jsonl"):
        return None
    suffix = stem[len("fills") : -len(".jsonl")]
    candidate = fills_path.parent / f"markouts{suffix}.jsonl"
    return candidate if candidate.is_file() else None


def _signed_bps(row: dict, key: str) -> float | None:
    fill_mid = row.get("fill_mid")
    mid = row.get(key)
    if not fill_mid or not mid:
        return None
    try:
        fill_mid, mid = float(fill_mid), float(mid)
    except (TypeError, ValueError):
        return None
    if fill_mid <= 0:
        return None
    side = str(row.get("side") or "").lower()
    if side == "buy":
        delta = mid - fill_mid
    elif side == "sell":
        delta = fill_mid - mid
    else:
        return None
    return delta / fill_mid * 10000.0


def aggregate(rows: Iterable[dict]) -> list[MarkoutPoint]:
    """One point per horizon. Horizons with no usable sample report None
    rather than 0.0 — "the mid didn't move" and "we don't know where the mid
    went" are different claims, and only one of them is good news."""
    rows = list(rows)
    out: list[MarkoutPoint] = []
    for key, label in HORIZONS:
        alls: list[float] = []
        buys: list[float] = []
        sells: list[float] = []
        for r in rows:
            v = _signed_bps(r, key)
            if v is None:
                continue
            alls.append(v)
            (buys if str(r.get("side") or "").lower() == "buy" else sells).append(v)
        out.append(
            {
                "horizon": label,
                "bps": statistics.fmean(alls) if alls else None,
                "median_bps": statistics.median(alls) if alls else None,
                "buy_bps": statistics.fmean(buys) if buys else None,
                "sell_bps": statistics.fmean(sells) if sells else None,
                "n": len(alls),
            }
        )
    return out
