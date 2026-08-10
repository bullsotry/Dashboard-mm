"""Performance metrics derived from a fills window. Pure functions, no I/O,
no venue coupling — anything with a list of Fill can use this.

WHAT THESE NUMBERS ARE, AND ARE NOT
-----------------------------------
Every figure here is computed over *the fills that were passed in*, which is
a bounded, truncated window (the file adapter keeps the last N fills). That
has three consequences worth stating plainly, because a market-making PnL
that quietly lies is worse than no PnL at all:

1. `realised_gross` is FIFO round-trip PnL. If the window opens mid-position,
   the lots that opened it are outside the window, so the first closes match
   against lots this function never saw. It handles that by only realising
   against inventory it has actually observed — unmatched closes start a new
   opposite lot instead of inventing an entry price. The effect is that
   realised PnL is *understated* right after a restart, converging as the
   window fills with complete round trips.

2. It is realised only. Open inventory is deliberately not marked to market
   here — that would mix a settled figure with a floating one in the same
   number. `inventory_base` is reported separately so the caller can see how
   much exposure sits outside the realised figure.

3. Fees are whatever the venue reported per fill. `realised_net` subtracts
   them. For a maker strategy this is usually the difference between a
   positive and a negative number, so gross alone is not a result.

`span_s` is the real time distance between the first and last fill used, so
the caller can label the window honestly instead of calling it "today".
"""

from __future__ import annotations

from collections import deque

from .base import Fill, Stats

_EPS = 1e-12


def _empty() -> Stats:
    return {
        "span_s": 0.0,
        "n_fills": 0,
        "n_buys": 0,
        "n_sells": 0,
        "volume_quote": 0.0,
        "fills_per_hour": 0.0,
        "realised_gross": 0.0,
        "fees": 0.0,
        "realised_net": 0.0,
        "inventory_base": 0.0,
        "capture_bps": None,
        "pnl_unreliable": None,
    }


def _replay(ordered: list[Fill]):
    """The FIFO round-trip matching engine, run once. Yields one entry per
    valid fill, in order, each carrying the effect that fill had — both
    `compute_stats` (which only wants the totals) and `equity_curve` (which
    wants the running series) are built on this single pass, so the matching
    rules exist in exactly one place.

    Signed inventory is kept as a FIFO queue of [qty, price] lots: positive
    qty means long lots, negative means short lots, and the queue never
    holds both signs at once because any opposite-side fill matches before
    it opens.
    """
    lots: deque[list[float]] = deque()
    for f in ordered:
        side = (f.get("side") or "").lower()
        if side not in ("buy", "sell"):
            continue
        price = float(f.get("price") or 0.0)
        size = float(f.get("size") or 0.0)
        if size <= 0 or price <= 0:
            continue

        fee = float(f.get("fee") or 0.0)
        realised_delta = 0.0
        signed = size if side == "buy" else -size
        remaining = signed

        # Close against opposite-sign lots first, realising as we go.
        while abs(remaining) > _EPS and lots and (lots[0][0] > 0) != (remaining > 0):
            lot_qty, lot_price = lots[0]
            matched = min(abs(remaining), abs(lot_qty))
            # Long lot closed by a sell earns (exit - entry); short lot
            # closed by a buy earns (entry - exit). The sign of lot_qty
            # collapses both into one expression.
            direction = 1.0 if lot_qty > 0 else -1.0
            realised_delta += direction * (price - lot_price) * matched

            lot_qty -= direction * matched
            remaining += direction * matched
            if abs(lot_qty) <= _EPS:
                lots.popleft()
            else:
                lots[0][0] = lot_qty

        # Whatever is left opens (or extends) a lot on this side.
        if abs(remaining) > _EPS:
            lots.append([remaining, price])

        yield {
            "ts": f["ts"],
            "side": side,
            "price": price,
            "size": size,
            "fee": fee,
            "realised_delta": realised_delta,
            "inventory_base": sum(q for q, _ in lots),
        }


def compute_stats(
    fills: list[Fill],
    net_position_base: float | None = None,
    hedge_mode: bool = False,
) -> Stats:
    """`net_position_base` and `hedge_mode` are not used in the arithmetic —
    they are used to decide whether the arithmetic is trustworthy at all.
    See `_reliability` below."""
    if not fills:
        return _empty()

    ordered = sorted(fills, key=lambda f: f["ts"])

    realised = 0.0
    fees = 0.0
    n_buys = n_sells = 0
    volume_quote = 0.0
    buy_base = buy_notional = 0.0
    sell_base = sell_notional = 0.0
    inventory_base = 0.0
    n_fills = 0

    for step in _replay(ordered):
        n_fills += 1
        fees += step["fee"]
        realised += step["realised_delta"]
        inventory_base = step["inventory_base"]
        volume_quote += step["price"] * step["size"]
        if step["side"] == "buy":
            n_buys += 1
            buy_base += step["size"]
            buy_notional += step["price"] * step["size"]
        else:
            n_sells += 1
            sell_base += step["size"]
            sell_notional += step["price"] * step["size"]

    # Bounds of the raw window, not of the valid rows within it — matches
    # the pre-refactor behaviour: a window that is entirely malformed fills
    # still reports the span it covered, just with every other figure at 0.
    span_s = ordered[-1]["ts"] - ordered[0]["ts"]
    fills_per_hour = (n_fills / span_s * 3600.0) if span_s > 0 else 0.0

    capture_bps = None
    if buy_base > 0 and sell_base > 0:
        avg_buy = buy_notional / buy_base
        avg_sell = sell_notional / sell_base
        mid = (avg_buy + avg_sell) / 2.0
        if mid > 0:
            capture_bps = (avg_sell - avg_buy) / mid * 10000.0

    return {
        "span_s": span_s,
        "n_fills": n_fills,
        "n_buys": n_buys,
        "n_sells": n_sells,
        "volume_quote": volume_quote,
        "fills_per_hour": fills_per_hour,
        "realised_gross": realised,
        "fees": fees,
        "realised_net": realised - fees,
        "inventory_base": inventory_base,
        "capture_bps": capture_bps,
        "pnl_unreliable": _reliability(inventory_base, net_position_base, hedge_mode),
    }


def equity_curve(
    fills: list[Fill],
    net_position_base: float | None = None,
    hedge_mode: bool = False,
) -> list[dict]:
    """The same FIFO replay as `compute_stats`, but returning the running
    series instead of only the final totals — one point per valid fill:
    `ts`, cumulative `realised_net`, and `inventory_base` at that point.

    This is what a Curve (cumulative realised PnL), a Delta (inventory over
    time) and a Volume curve (cumulative traded notional) panel are built
    from. `cum_volume_quote` is a plain running sum of price*size per fill —
    unlike realised PnL and inventory, it does not depend on FIFO lot
    matching, so it stays correct even on a window `_reliability` below
    flags as unreliable; it still carries the same `pnl_unreliable` verdict
    on every point purely so callers get one flag per point instead of
    juggling two.
    """
    ordered = sorted(fills, key=lambda f: f["ts"])

    out = []
    cum_realised = 0.0
    cum_fees = 0.0
    cum_volume_quote = 0.0
    for step in _replay(ordered):
        cum_realised += step["realised_delta"]
        cum_fees += step["fee"]
        cum_volume_quote += step["price"] * step["size"]
        out.append(
            {
                "ts": step["ts"],
                "realised_net": cum_realised - cum_fees,
                "inventory_base": step["inventory_base"],
                "cum_volume_quote": cum_volume_quote,
            }
        )

    # Same verdict `compute_stats` would reach on this window (hedge mode
    # doesn't depend on the replay's outcome; the inventory-gap check needs
    # the final inventory, which only exists once the loop above has run).
    final_inventory = out[-1]["inventory_base"] if out else 0.0
    unreliable = _reliability(final_inventory, net_position_base, hedge_mode)

    return [{**p, "pnl_unreliable": unreliable} for p in out]


# Below this, a base-units gap between the ledger and the exchange is treated
# as rounding rather than a missing history.
_INVENTORY_TOLERANCE = 0.05


def _reliability(
    inventory_base: float,
    net_position_base: float | None,
    hedge_mode: bool,
) -> str | None:
    """Why the realised figures above must not be trusted, or None.

    This exists because the realised PnL was wrong in production and looked
    confident while being so. Two conditions make it structurally wrong, and
    neither is detectable from the fills alone:

    1. **The ledger is incomplete.** If replaying every fill lands on an
       inventory the exchange disagrees with, the window opened mid-position
       and the lots that opened it were never recorded. Closes then match
       against lots invented by the replay, at prices that were never paid.
       Observed live: a ledger implying -5.78 base against a real +0.22.

    2. **Hedge mode.** With a long and a short open at once, a buy may close
       the short or add to the long, and the ledger records no flag saying
       which. FIFO netting collapses two independent books into one and
       realises round trips the exchange never settled.

    The symptom either way is a figure that swings with the window size —
    the same fills gave -1.78, -14.05 and -8.84 over 500, 2000 and all
    2113 rows. A number that depends on where you cut is not a measurement.
    """
    if hedge_mode:
        return "hedge mode: fills carry no position side, so FIFO netting invents round trips"
    if net_position_base is None:
        return None
    gap = abs(inventory_base - net_position_base)
    if gap > _INVENTORY_TOLERANCE:
        return (
            f"ledger incomplete: replay implies {inventory_base:+.2f} base, "
            f"exchange holds {net_position_base:+.2f}"
        )
    return None
