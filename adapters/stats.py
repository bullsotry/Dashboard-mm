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

THE CASH-FLOW FIGURE, AND WHY IT EXISTS ALONGSIDE THE FIFO ONE
-------------------------------------------------------------
`realised_net` is refused outright in hedge mode, because FIFO netting
cannot know whether a buy closed a short or added to a long. On this fleet
that is not an edge case: the Bitunix leg runs a long and a short at once as
a matter of course, so its main venue would show no result at all — and a
dashboard that can never answer "how did tonight go" for the leg doing the
volume is not much of an improvement over one that answers wrong.

`cash_pnl` answers it without matching anything:

    (money in from sells) - (money out on buys)
    + (closing inventory valued at the closing mark)
    - (opening inventory valued at the opening mark)
    - fees

Every term is a sum over fills or a single multiplication. No lot matching,
so hedge mode does not affect it, and neither does a truncated pre-roll: the
opening inventory is taken from what the *bot itself* recorded alongside its
first fill of the session (`inventory_before`), not from the replay.

It is not the same number as `realised_net` and must never be relabelled as
one: it marks the inventory the session was carrying to market, so it moves
with price on any session that does not end flat. For a market maker ending
roughly flat the two converge; when they don't, the difference is exactly the
open exposure. Both are shown, both are labelled.

MEASURING ONE SESSION
---------------------
`since_ts` narrows every figure to a single trading session (see
`adapters/sessions.py` for where a session's bounds come from) without
narrowing the *replay*. That distinction is the whole point: a bot almost
never restarts flat, so a replay that simply dropped every fill before the
session start would begin at inventory 0 while the exchange holds a real
position, and the session's first sells would match against lots the bot
never bought — the same fiction `_reliability` exists to refuse.

So fills before `since_ts` are still replayed, to rebuild the FIFO lots the
session opened with; they just don't contribute to any total. What the
session reports is the effect of its own fills on lots priced at what was
actually paid for them, whenever that was.

That rebuild is only as good as how far back the ledger reaches, so it is
checked rather than assumed: `opening_inventory_base` is what the replay
believes the session opened with, and the caller compares it against the
`inventory_before` the ledger recorded on the session's first fill. They
disagree -> the pre-roll was too short, and the session's realised figure is
refused like any other unverifiable one.
"""

from __future__ import annotations

import math
from collections import deque

from .base import Fill, Stats

_EPS = 1e-12

# Beyond this share of one-sided flow, `capture_bps` stops measuring spread
# capture. 0.10 = a 55/45 split; the live OKX window sat at 0.31 (612 buys
# against 319 sells) and still printed a confident +9.7 bps.
_CAPTURE_MAX_IMBALANCE = 0.10

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
        "capture_unavailable": None,
        "pnl_unverified": None,
        "pnl_unreliable": None,
        "opening_inventory_base": 0.0,
        "closing_inventory_base": 0.0,
        "n_preroll_fills": 0,
        "cash_pnl": None,
        "cash_pnl_basis": None,
        "cash_pnl_unavailable": None,
        "closing_mark": None,
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
            # Passed through untouched for the cash-flow figure: the venue
            # mid at the moment of the fill (a better mark than the fill's
            # own price, which sits a half-spread off it), and the bot's own
            # inventory reading just before it. Either may be absent.
            "mid_at_fill": f.get("mid_at_fill"),
            "inventory_before": f.get("inventory_before"),
        }


def compute_stats(
    fills: list[Fill],
    net_position_base: float | None = None,
    hedge_mode: bool = False,
    since_ts: float | None = None,
    until_ts: float | None = None,
    opening_inventory_recorded: float | None = None,
) -> Stats:
    """`net_position_base` and `hedge_mode` are not used in the arithmetic —
    they are used to decide whether the arithmetic is trustworthy at all.
    See `_reliability` below.

    `since_ts` restricts what is *counted* to one session while still
    replaying everything before it, so the session opens on real lots — see
    the module docstring.
    """
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
    n_preroll = 0
    opening_inventory = 0.0
    closing_inventory = 0.0
    opening_mark: float | None = None
    closing_mark: float | None = None
    opening_recorded: float | None = None
    closing_recorded: float | None = None
    signed_since_open = 0.0  # net base bought since the window's first fill
    signed_after_window = 0.0  # net base bought after the window closed
    ledger_signed_size = 0.0
    cash_in = 0.0  # proceeds of sells
    cash_out = 0.0  # cost of buys
    first_counted_ts: float | None = None
    last_counted_ts: float | None = None

    for step in _replay(ordered):
        inventory_base = step["inventory_base"]
        if since_ts is not None and step["ts"] < since_ts:
            # Replayed for its effect on the lots, contributes to no total.
            n_preroll += 1
            opening_inventory = inventory_base
            continue
        # Past the end of a finished session. The loop is not broken out of:
        # `inventory_base` must keep tracking to the present, because it is
        # what `_reliability` checks against the exchange's *current*
        # position — a session's closing inventory is not what the exchange
        # holds now.
        if until_ts is not None and step["ts"] > until_ts:
            # Past the window, but its effect on inventory still matters:
            # anchoring a finished session on the position held *now* means
            # walking back through everything traded since it ended.
            signed_after_window += step["size"] if step["side"] == "buy" else -step["size"]
            continue
        if first_counted_ts is None:
            first_counted_ts = step["ts"]
            opening_mark = step["mid_at_fill"] or step["price"]
            opening_recorded = step["inventory_before"]
        last_counted_ts = step["ts"]
        closing_mark = step["mid_at_fill"] or step["price"]
        closing_inventory = inventory_base
        # The ledger's own reading on the last counted fill, and the net
        # movement the fills between the first and this one should have
        # caused. Compared in `_ledger_inventory_in_base_units` — they must
        # agree, or the two fields are counting different things.
        if step["inventory_before"] is not None:
            closing_recorded = step["inventory_before"]
            ledger_signed_size = signed_since_open
        signed_since_open += step["size"] if step["side"] == "buy" else -step["size"]
        n_fills += 1
        fees += step["fee"]
        realised += step["realised_delta"]
        volume_quote += step["price"] * step["size"]
        if step["side"] == "buy":
            n_buys += 1
            buy_base += step["size"]
            buy_notional += step["price"] * step["size"]
            cash_out += step["price"] * step["size"]
        else:
            n_sells += 1
            sell_base += step["size"]
            sell_notional += step["price"] * step["size"]
            cash_in += step["price"] * step["size"]

    if since_ts is not None:
        # A session's span is its own duration, not the distance between its
        # first and last fill: a bot that ran an hour before trading was
        # still running for that hour, and dividing by the shorter span
        # would flatter its fill rate. A finished session knows its end; a
        # running one is measured to its latest fill (the caller has the
        # wall clock and reports elapsed time separately).
        if until_ts is not None:
            span_s = until_ts - since_ts
        else:
            span_s = (last_counted_ts - since_ts) if last_counted_ts is not None else 0.0
    else:
        # Bounds of the raw window, not of the valid rows within it — matches
        # the pre-refactor behaviour: a window that is entirely malformed fills
        # still reports the span it covered, just with every other figure at 0.
        span_s = ordered[-1]["ts"] - ordered[0]["ts"]
    fills_per_hour = (n_fills / span_s * 3600.0) if span_s > 0 else 0.0

    # Cash-flow PnL — see the module docstring. Independent of FIFO matching,
    # so it survives hedge mode; dependent instead on knowing what the window
    # opened and closed holding, and at what mark.
    #
    # The opening inventory comes from the bot's own record on the session's
    # first fill when it has one; only if it doesn't is the replay's rebuilt
    # figure used, and `cash_pnl_basis` says which, because the two carry
    # different guarantees.
    cash_pnl = None
    cash_pnl_basis = None
    cash_pnl_unavailable = None
    ledger_usable = _ledger_inventory_in_base_units(
        opening_recorded, closing_recorded, ledger_signed_size
    )
    if n_fills and opening_mark and closing_mark:
        # Anchored on the exchange's own position, walked backwards through
        # the fills, in preference to anything the ledger says about
        # inventory. That field was measured wrong on this fleet: it read
        # +0.00 at the start of a session the bot actually opened 2.86 base
        # short, and sat frozen at 0.00 across three earlier sessions. The
        # exchange position is the one inventory figure here that does not
        # come from the ledger at all.
        #
        # This holds as long as the session's fills are complete, which was
        # verified against the venue's own trade history on 2026-08-12: 692
        # trades reported, 692 in the ledger, none missing. It is not
        # re-verified on every poll — that would need a signed API call per
        # render — so a ledger that later starts dropping fills would push
        # the error into the opening inventory instead of announcing itself.
        # `closing_inventory_base` vs the position is what would show it.
        # `ledger_usable` gates this too: when a bot's inventory fields are
        # denominated in contracts rather than base (OKX: 1026 contracts =
        # 10.26 SOL), its *position* is in those same units, so anchoring on
        # it would mark a 10-SOL book as a 1026-SOL one. Live check before
        # this gate was added: OKX read -76.60 USD on a 777 USD session.
        if net_position_base is not None and ledger_usable:
            close_inv = net_position_base - signed_after_window
            open_inv = close_inv - (buy_base - sell_base)
            cash_pnl_basis = "exchange position"
            # The anchored figure is the honest one to show alongside it:
            # the replay's own closing inventory is the thing we just decided
            # not to trust.
            closing_inventory = close_inv
            cash_pnl = (
                (cash_in - cash_out)
                + close_inv * closing_mark
                - open_inv * opening_mark
                - fees
            )
        elif opening_recorded is not None and not ledger_usable:
            # The ledger records an inventory, but not in the same unit as
            # its own fill sizes — OKX rows count contracts while `size` is
            # in base, so the position moves 100x faster than the fills that
            # caused it. Marking that number would value a 10-SOL book as a
            # 1026-SOL one. Refuse: this is a units bug in the ledger, and
            # guessing the multiplier would be inventing data.
            cash_pnl_unavailable = (
                "ledger inventory is not in the same units as its fill sizes: "
                f"it moved {abs(closing_recorded - opening_recorded):.0f} while the "
                f"fills moved {abs(ledger_signed_size):.2f} base"
            )
        elif opening_recorded is not None:
            open_inv, cash_pnl_basis = opening_recorded, "ledger"
        elif since_ts is None:
            # A full-history window opens flat by construction: there is
            # nothing before the first fill it holds.
            open_inv, cash_pnl_basis = 0.0, "window start"
        else:
            open_inv, cash_pnl_basis = opening_inventory, "replay"

        if cash_pnl_basis is not None:
            close_inv = open_inv + (buy_base - sell_base)
            # The closing inventory is *inferred* from the fills, so it is
            # only as complete as they are — and a missing fill is worth its
            # full notional here, not a rounding error. Checked against what
            # the exchange says the bot holds, which is the one figure in
            # this whole pipeline that doesn't come from the ledger.
            #
            # Live 2026-08-12: the fills implied +5.90 base against a real
            # +3.41, so 2.49 SOL of inventory that does not exist was marked
            # at 75.56 and the session read -1.24 USD instead of -189.38.
            # Neither number is the truth — the fills that went missing
            # carried cash too — which is exactly why this refuses rather
            # than substituting the exchange's figure and calling it fixed.
            cash_pnl = (
                (cash_in - cash_out)
                + close_inv * closing_mark
                - open_inv * opening_mark
                - fees
            )

    # Capture only means "spread captured" when the two sides are roughly
    # balanced. On a one-sided window `avg_sell - avg_buy` is measuring where
    # price went between the buying and the selling, not what the quotes
    # earned — the live OKX window read +9.7 bps off 612 buys against 319
    # sells, which is a trend, not a capture. Above the imbalance limit the
    # figure is withheld rather than shown with a caveat nobody reads.
    capture_bps = None
    capture_unavailable = None
    if buy_base > 0 and sell_base > 0:
        imbalance = abs(buy_base - sell_base) / (buy_base + sell_base)
        if imbalance > _CAPTURE_MAX_IMBALANCE:
            capture_unavailable = (
                f"flow {100 * imbalance:.0f}% one-sided: this would measure "
                f"price drift, not captured spread"
            )
        else:
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
        "capture_unavailable": capture_unavailable,
        "pnl_unverified": _unverified(net_position_base, hedge_mode),
        # Checked before `_reliability`: a counter gap is *proof* a fill is
        # missing, where the inventory checks below only infer it — and when
        # both fire, the gap is the cause and the inventory mismatch its
        # symptom, so naming the symptom would send the reader looking in the
        # wrong place.
        "pnl_unreliable": _sequence_gap(ordered, since_ts, until_ts)
        or _reliability(
            inventory_base,
            net_position_base,
            hedge_mode,
            _opening_gap(since_ts, opening_inventory, opening_inventory_recorded),
        ),
        # What the replay believes the session opened holding, and how many
        # fills it had to walk to get there. The caller checks the first
        # against the ledger's own `inventory_before`; the second is what
        # tells "opened flat" apart from "pre-roll found nothing".
        "opening_inventory_base": opening_inventory,
        # What the window ended holding. Identical to `inventory_base` for a
        # running session; for a finished one they differ, and showing the
        # current inventory in a panel headed by last night's session would
        # be the same category of lie this module exists to prevent.
        "closing_inventory_base": closing_inventory,
        "n_preroll_fills": n_preroll,
        # Net of fees, and marked to market on whatever inventory the window
        # ended carrying — not a realised figure, and labelled as such
        # wherever it is shown.
        "cash_pnl": cash_pnl,
        "cash_pnl_basis": cash_pnl_basis,
        "cash_pnl_unavailable": cash_pnl_unavailable,
        "closing_mark": closing_mark,
    }


# How many times larger the ledger's inventory movement must be than the
# fills that caused it before the two are judged to be counting different
# things. This tests for a *scale* mismatch and nothing else, because the
# readings are also sampled asynchronously: on the Bitunix ledger they lag
# far enough that a session's recorded inventory can barely move while its
# fills net out to several base units. That is noise, not a units bug, and
# an earlier symmetric tolerance rejected it — throwing away the three
# sessions that had been cross-validated to within 0.15 USD against the
# account's own equity delta. A contract-denominated field, by contrast,
# overshoots by 40-100x.
_UNITS_SCALE_FACTOR = 5.0


def _ledger_inventory_in_base_units(
    opening_recorded: float | None,
    closing_recorded: float | None,
    net_signed_size: float,
) -> bool:
    """Does the ledger's inventory move on the same scale as the fills that
    moved it? If a window's fills net out to +0.2 base, the recorded
    inventory cannot have climbed by 20 — observed on OKX rows, where that
    field counts contracts (1 = 0.01 base) while `size` counts base.

    Only an overshoot is a failure. The readings lagging *behind* the fills
    is ordinary asynchronous sampling and says nothing about units.

    Returns True when there is nothing to contradict (no readings, or a
    window too small to tell): the caller only uses this to *refuse*, so
    "unknown" must not read as "broken".
    """
    if opening_recorded is None or closing_recorded is None:
        return True
    ledger_move = abs(closing_recorded - opening_recorded)
    fills_move = abs(net_signed_size)
    if ledger_move < _INVENTORY_TOLERANCE:
        return True  # barely moved: nothing to disagree with
    return ledger_move <= _UNITS_SCALE_FACTOR * max(fills_move, _INVENTORY_TOLERANCE)


def _unverified(net_position_base: float | None, hedge_mode: bool) -> str | None:
    """Why the realised figure could not be *checked*, or None.

    Deliberately separate from `_reliability`. That one answers "is this
    known to be wrong", and its answer withholds the number. This one
    answers "was this ever verified", and its answer only annotates it —
    the figure is still shown, because there is no evidence against it.

    Conflating the two is what produced the worst reading on this dashboard:
    the Coinbase leg publishes no position, so the check could not run, so
    the check reported nothing, so its PnL was the one figure on screen
    displayed as trustworthy — precisely because it was the only one that
    could not be tested.
    """
    if hedge_mode or net_position_base is not None:
        return None
    return "unverified: this bot publishes no position, so the replay can't be checked against the exchange"


def _sequence_gap(
    fills: list[Fill],
    since_ts: float | None = None,
    until_ts: float | None = None,
) -> str | None:
    """Fills the recorder never captured, counted from the engine's own
    per-fill counter.

    Everything else in this module *infers* an incomplete ledger, by noticing
    that the replay lands somewhere the exchange disagrees with. That
    inference is late (it needs a position to compare against) and mute about
    size — it cannot say a fill went missing rather than the position having
    moved for some other reason. `fill_seq` is direct evidence: the engine
    stamps every fill it records with a counter, the recorder samples that
    deque on a timer, so a jump in the counter is a fill that was traded and
    never written down.

    Scoped to the reported window, not the whole ledger: a hole from three
    days ago does not make tonight's session unmeasurable, and the lots it
    left behind are already what `_opening_gap` checks.

    Claims nothing about rows with no counter (written before the engine
    emitted one) and nothing across a change of `fill_run_id` — a restart
    resets the counter to 1, which is not a gap. Fills lost *while the bot
    was down* are invisible to this check by construction; it proves a
    ledger has holes, never that it has none.
    """
    prev_seq: float | None = None
    prev_run: float | None = None
    missing = 0
    first_gap: tuple[int, int] | None = None
    for f in fills:
        if since_ts is not None and f["ts"] < since_ts:
            continue
        if until_ts is not None and f["ts"] > until_ts:
            continue
        seq = f.get("fill_seq")
        run = f.get("fill_run_id")
        if seq is None:
            prev_seq = prev_run = None
            continue
        if prev_seq is not None and run == prev_run:
            step = seq - prev_seq
            if step > 1:
                missing += int(step) - 1
                if first_gap is None:
                    first_gap = (int(prev_seq), int(seq))
        prev_seq, prev_run = seq, run

    if missing and first_gap is not None:
        return (
            f"ledger incomplete: {missing} fill(s) the recorder never wrote down "
            f"(the engine's counter jumps {first_gap[0]} -> {first_gap[1]})"
        )
    return None


def _opening_gap(
    since_ts: float | None,
    replayed: float,
    recorded: float | None,
) -> tuple[float, float] | None:
    """The pair `_reliability` compares, or None when there is nothing to
    compare: no session boundary, or a ledger that records no inventory
    alongside its fills (Coinbase's rows carry `inventory_before: null`)."""
    if since_ts is None or recorded is None:
        return None
    return (replayed, recorded)


def equity_curve(
    fills: list[Fill],
    net_position_base: float | None = None,
    hedge_mode: bool = False,
    since_ts: float | None = None,
    until_ts: float | None = None,
    opening_inventory_recorded: float | None = None,
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

    `drawdown` and `max_drawdown` ride on the realised-PnL side of the same
    replay, so they inherit `pnl_unreliable` the same way `realised_net`
    does: a peak-to-trough drop computed on a curve that can't defend its
    own total is exactly as fictional as the total. `drawdown` is how far
    below the running peak the curve sits *at this point*; `max_drawdown` is
    the largest that has ever been, so far — both start at 0.0 (the curve's
    implicit starting value before the first fill), and `max_drawdown` is
    monotonically non-decreasing, so its value on the last point is the
    window's overall max drawdown.
    """
    ordered = sorted(fills, key=lambda f: f["ts"])

    out = []
    cum_realised = 0.0
    cum_fees = 0.0
    cum_volume_quote = 0.0
    peak = 0.0  # the curve starts at 0 before the first fill
    max_drawdown = 0.0
    opening_inventory = 0.0
    latest_inventory = 0.0
    for step in _replay(ordered):
        # Tracked across the *whole* replay, not just the session window:
        # `_reliability` checks it against what the exchange holds now, and
        # a finished session's closing inventory is not that.
        latest_inventory = step["inventory_base"]
        # Pre-roll: the lots are rebuilt (inside `_replay`), but the curve
        # itself starts at the session boundary. Carrying the previous
        # session's cumulative PnL into this one's first point would make
        # every session inherit the last one's result.
        if since_ts is not None and step["ts"] < since_ts:
            opening_inventory = step["inventory_base"]
            continue
        if until_ts is not None and step["ts"] > until_ts:
            continue
        cum_realised += step["realised_delta"]
        cum_fees += step["fee"]
        cum_volume_quote += step["price"] * step["size"]
        realised_net = cum_realised - cum_fees
        peak = max(peak, realised_net)
        drawdown = peak - realised_net
        max_drawdown = max(max_drawdown, drawdown)
        out.append(
            {
                "ts": step["ts"],
                "realised_net": realised_net,
                "inventory_base": step["inventory_base"],
                "cum_volume_quote": cum_volume_quote,
                "drawdown": drawdown,
                "max_drawdown": max_drawdown,
            }
        )

    # Same verdict `compute_stats` would reach on this window (hedge mode
    # doesn't depend on the replay's outcome; the inventory-gap check needs
    # the final inventory, which only exists once the loop above has run).
    unreliable = _sequence_gap(ordered, since_ts, until_ts) or _reliability(
        latest_inventory,
        net_position_base,
        hedge_mode,
        _opening_gap(since_ts, opening_inventory, opening_inventory_recorded),
    )

    return [{**p, "pnl_unreliable": unreliable} for p in out]


# Below this, a base-units gap between the ledger and the exchange is treated
# as rounding rather than a missing history.
_INVENTORY_TOLERANCE = 0.05


def _reliability(
    inventory_base: float,
    net_position_base: float | None,
    hedge_mode: bool,
    opening_gap: tuple[float, float] | None = None,
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
    # Checked before the closing gap because it is the *cause* of most of
    # them: a session whose opening lots were rebuilt wrong closes wrong too,
    # and naming the closing symptom would send the reader looking in the
    # wrong place.
    if opening_gap is not None:
        replayed, recorded = opening_gap
        if abs(replayed - recorded) > _INVENTORY_TOLERANCE:
            return (
                f"session opened mid-position and the ledger doesn't reach back "
                f"far enough: replay rebuilt {replayed:+.2f} base at the start, "
                f"the ledger recorded {recorded:+.2f}" + _scale_hint(replayed, recorded)
            )
    if net_position_base is None:
        # Nothing here can be proven wrong, so nothing is refused — but see
        # `_unverified` below: "no evidence against" is not "checked".
        return None
    gap = abs(inventory_base - net_position_base)
    if gap > _INVENTORY_TOLERANCE:
        return (
            f"ledger incomplete: replay implies {inventory_base:+.2f} base, "
            f"exchange holds {net_position_base:+.2f}"
            + _scale_hint(inventory_base, net_position_base)
        )
    return None


def _scale_hint(replayed: float, reported: float) -> str:
    """Names the one cause of a mismatch that isn't a missing history: the
    two sides counting in different units.

    A ledger that has genuinely lost fills lands on an arbitrary gap. Two
    numbers separated by a round factor of 100 have not lost anything —
    that is contracts against base units, which is what the OKX engine
    published for months (`net_position_base: 2489` for 24.89 SOL). The
    message pointed at the ledger and the reader went looking in the wrong
    file.

    Only a power of ten of 10 or more, matched to within 0.5%, earns the
    hint. Contract values are powers of ten (0.01 SOL here), so that is the
    shape a unit bug actually has; any other ratio is what a real hole looks
    like. An earlier version accepted any round-ish integer and announced
    "an exact factor of 111" on a window that was 111.5x — a number that is
    neither exact nor a unit — which is precisely the confident guess this
    module exists to refuse.
    """
    if abs(replayed) < _INVENTORY_TOLERANCE or abs(reported) < _INVENTORY_TOLERANCE:
        return ""
    ratio = abs(reported / replayed)
    if ratio < 1.0:
        ratio = 1.0 / ratio
    if ratio < 9.0:
        return ""
    power = 10 ** round(math.log10(ratio))
    if power >= 10 and abs(ratio - power) <= 0.005 * power:
        return (
            f" — an exact factor of {power} between the two, which looks like "
            f"a unit mismatch (contracts vs base) rather than a missing history"
        )
    return ""
