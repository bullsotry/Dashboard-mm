"""Inter-venue basis: the mid-price gap between two legs of the same asset.

Pure, no I/O — takes whatever mids the caller already has in hand (from the
orderbooks it already reads every poll) and pairs them up. This is the one
number generic exchange dashboards don't show and this fleet needs: a cross-
venue MM bot (Bitunix leg quoting, Coinbase leg pricing/hedging, or vice
versa) can drift apart from its own reference without either leg looking
individually wrong.

A MID IS ONLY WORTH AS MUCH AS IT IS FRESH
------------------------------------------
A leg's mid comes from the bot's own published orderbook, which stops
advancing the moment the bot stops. Nothing about a frozen mid looks wrong:
it is a well-formed number, of the right magnitude, for the right symbol.
Paired against a live leg it produces a large, stable, plausible-looking
basis that is pure fiction, and whose sparkline even *moves* — because the
live leg moves. Observed on this dashboard 2026-08-12: +517.9 bps between a
live Bitunix leg and a shadow bot whose file had been frozen for five days,
and +85.9 bps against an OKX leg dead for 30 hours.

So freshness is not a display detail here, it is part of the arithmetic:
`split_stale` drops any leg the caller can't vouch for, and every surviving
pair carries `skew_s` — how far apart in time the two mids were actually
sampled. That last part matters even when both legs are alive: on this fleet
the Bitunix mid is ~0.3s old while the Coinbase one is ~12s old (measured
2026-08-12, n=40), and 12s of SOL movement is worth 1-2 bps — the same order
of magnitude as the basis being measured. A basis quoted without its skew is
a number without a protocol.
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
    ts: float  # when the venue stamped this mid, not when we read it


class StaleLeg(TypedDict):
    key: str
    age_s: float | None  # None when the leg published no timestamp at all
    reason: str


class BasisPair(TypedDict):
    base: str
    a: str  # leg key
    b: str  # leg key
    label: str
    bps: float  # (mid_a - mid_b) / mid_b * 10000
    skew_s: float  # |ts_a - ts_b|: how unsynchronised the two mids are
    age_s: float  # age of the older of the two mids at `now`


def split_stale(
    legs: list[Leg], now: float, max_age_s: float
) -> tuple[list[Leg], list[StaleLeg]]:
    """Partition legs into those whose mid is fresh enough to compare and
    those that are not, with a reason for each exclusion.

    A leg with no `ts` is excluded rather than trusted: the whole failure
    mode this guards against is a mid that looks perfectly valid while being
    hours old, and an absent timestamp is exactly the case where we cannot
    tell. Refusing is the same choice `stats._reliability` makes.
    """
    fresh: list[Leg] = []
    stale: list[StaleLeg] = []
    for leg in legs:
        ts = leg.get("ts")
        if not ts:
            stale.append({"key": leg["key"], "age_s": None, "reason": "no timestamp"})
            continue
        age = now - float(ts)
        if age > max_age_s:
            stale.append({"key": leg["key"], "age_s": age, "reason": "stale mid"})
            continue
        fresh.append(leg)
    return fresh, stale


def _pair_label(a: Leg, b: Leg, ambiguous: set[str]) -> str:
    """"bitunix vs okx" when that names exactly one pair, and the full bot
    keys when it doesn't.

    `ambiguous` holds the exchanges contributing more than one leg to this
    base asset. Naming by exchange alone is the readable form and stays the
    default; it just cannot be used when two different bots on that venue
    would both answer to it.
    """
    def name(leg: Leg) -> str:
        return leg["key"] if leg["exchange"] in ambiguous else leg["exchange"]

    return f"{name(a)} vs {name(b)}"


def compute_basis_pairs(legs: list[Leg], now: float | None = None) -> list[BasisPair]:
    """One entry per pair of legs on *different* exchanges that share a base
    asset and both have a live mid. Two legs on the same exchange are never
    paired — that would be a symbol-naming collision, not a basis.

    This does not filter on freshness — `split_stale` does, and callers are
    expected to run it first. Keeping the two apart means the pairing rules
    stay testable without a clock.
    """
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
        seen_ex: dict[str, int] = defaultdict(int)
        for leg in ordered:
            seen_ex[leg["exchange"]] += 1
        ambiguous = {ex for ex, n in seen_ex.items() if n > 1}
        for i in range(len(ordered)):
            for j in range(i + 1, len(ordered)):
                a, b = ordered[i], ordered[j]
                if a["exchange"] == b["exchange"]:
                    continue
                bps = (a["mid"] - b["mid"]) / b["mid"] * 10000.0
                ts_a, ts_b = float(a.get("ts") or 0.0), float(b.get("ts") or 0.0)
                skew_s = abs(ts_a - ts_b) if (ts_a and ts_b) else 0.0
                oldest = min(ts_a, ts_b) if (ts_a and ts_b) else 0.0
                pairs.append(
                    {
                        "base": base,
                        "a": a["key"],
                        "b": b["key"],
                        # Keyed by bot, not by exchange: two bots on the
                        # same venue (a live one and a shadow) produced two
                        # rows both labelled "bitunix vs okx" carrying
                        # +87.2 and +519.3 bps. Same words, different
                        # numbers, no way to tell which was which.
                        "label": _pair_label(a, b, ambiguous),
                        "bps": bps,
                        "skew_s": skew_s,
                        "age_s": (now - oldest) if (now and oldest) else 0.0,
                    }
                )
    return pairs
