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
    }


def compute_stats(fills: list[Fill]) -> Stats:
    if not fills:
        return _empty()

    ordered = sorted(fills, key=lambda f: f["ts"])

    # Signed inventory as a FIFO queue of [qty, price] lots. Positive qty
    # means long lots, negative means short lots; the queue never holds both
    # signs at once because any opposite-side fill matches before it opens.
    lots: deque[list[float]] = deque()
    realised = 0.0
    fees = 0.0
    n_buys = n_sells = 0
    volume_quote = 0.0
    buy_base = buy_notional = 0.0
    sell_base = sell_notional = 0.0

    for f in ordered:
        side = (f.get("side") or "").lower()
        if side not in ("buy", "sell"):
            continue
        price = float(f.get("price") or 0.0)
        size = float(f.get("size") or 0.0)
        if size <= 0 or price <= 0:
            continue

        fees += float(f.get("fee") or 0.0)
        volume_quote += price * size
        if side == "buy":
            n_buys += 1
            buy_base += size
            buy_notional += price * size
        else:
            n_sells += 1
            sell_base += size
            sell_notional += price * size

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
            realised += direction * (price - lot_price) * matched

            lot_qty -= direction * matched
            remaining += direction * matched
            if abs(lot_qty) <= _EPS:
                lots.popleft()
            else:
                lots[0][0] = lot_qty

        # Whatever is left opens (or extends) a lot on this side.
        if abs(remaining) > _EPS:
            lots.append([remaining, price])

    span_s = ordered[-1]["ts"] - ordered[0]["ts"]
    n_fills = n_buys + n_sells
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
        "inventory_base": sum(q for q, _ in lots),
        "capture_bps": capture_bps,
    }
