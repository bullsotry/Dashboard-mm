"""Reads one bot's published state: orderbook, positions, resting orders, fills.

Venue-agnostic on purpose. This was originally written as a Bitunix adapter,
but nothing in it was ever Bitunix-specific — it reads a JSON state file and
a JSONL fill ledger, both of which every bot in this fleet writes in the same
shape. Keeping one reader means a new venue costs nothing here; only the
market-data adapters (klines, account) are venue-specific.

Never writes to these files, never imports the bot's code, never touches the
bot's process. If a file is missing or briefly invalid (caught mid atomic
write), methods return None / [] rather than raising — a monitoring dashboard
must degrade quietly, not crash a poll loop.
"""

from __future__ import annotations

import functools
import json
import threading
from collections import deque
from pathlib import Path

from .base import Account, Fill, OrderBook, Position, Quote, Stats
from .markouts import aggregate, markouts_path_for
from .sessions import Session, build_sessions, parse_sessions_csv, sessions_csv_for
from .stats import compute_stats, equity_curve


def _opt_float(value) -> float | None:
    """A number, or None if the field is absent/null/unparseable. Distinct
    from `float(x or 0.0)`: here "the bot didn't record this" and "the bot
    recorded zero" must stay different, because one means unverifiable and
    the other means verified flat."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _synchronized(method):
    """Serialise one adapter instance's methods against each other.

    /snapshot is a sync `def`, so FastAPI runs it in the anyio threadpool:
    two tabs, or a forced poll landing on a pending one, put several threads
    inside the *same* cached adapter at once, and the background loops add
    another. Everything below tails file offsets and memoises a replay keyed
    on them, none of which is atomic.

    Measured before this existed (tests/test_concurrency.py): 8 threads
    against a 3000-row ledger each re-read the whole file from offset 0,
    leaving 20000 rows in a deque capped at 20000 and a realised PnL of
    ~9798 where the true figure is 1470 — with two threads disagreeing on
    which wrong number it was.

    Reentrant because these methods call each other (get_stats -> _all_fills
    -> _poll_fills). Every section it guards is local-file I/O measured in
    microseconds, never a network call, so serialising is cheap — and it
    collapses the duplicate work of two tabs asking for the same figure at
    the same instant into one computation.
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class BotStateAdapter:
    def __init__(
        self,
        venue_name: str,
        symbol: str,
        viz_path: Path,
        fills_paths: Path | list[Path] | tuple[Path, ...] | None,
        fills_maxlen: int = 2000,
    ):
        # Guards every method below; see _synchronized above.
        self._lock = threading.RLock()
        self.venue_name = venue_name
        self.symbol = symbol
        self._viz_path = viz_path
        if fills_paths is None:
            fills_paths = ()
        elif isinstance(fills_paths, Path):
            fills_paths = (fills_paths,)  # back-compat: a single path is still accepted
        self._fills_paths = tuple(fills_paths)
        self._fills_maxlen = fills_maxlen
        # Tailed independently per source file, keyed by path: a fleet
        # commonly splits fills across several ledgers written by different
        # trackers (one per venue), each running its own atomic-write/reset
        # lifecycle. Sharing one offset/inode across files would either miss
        # rows or wrongly treat one file's rotation as if it applied to all
        # of them.
        self._fills_by_path: dict[Path, deque[Fill]] = {
            p: deque(maxlen=fills_maxlen) for p in self._fills_paths
        }
        # Base units per contract, as declared by the tracker on every fill
        # row it writes. 1.0 on venues that trade in base units directly
        # (Bitunix, Coinbase); 0.01 on OKX SOL XPERP, where the engine counts
        # contracts. It is read from the ledger rather than configured here
        # because the tracker gets it from the venue's own instrument
        # definition — a second copy in this repo would be a guess that goes
        # stale the day the bot changes instrument.
        #
        # The default of 1.0 is the safe direction: on a ledger that doesn't
        # declare it, the viz position stays unconverted, the replay
        # disagrees with it and the panel refuses. Never a converted number
        # nobody can defend.
        self._contract_value: float = 1.0
        self._fills_offset: dict[Path, int] = {p: 0 for p in self._fills_paths}
        self._fills_inode: dict[Path, int | None] = {p: None for p in self._fills_paths}
        # (stat identity, parsed json) for the viz file — see _read_viz.
        self._viz_cache: tuple[tuple[int, int, int], dict] | None = None
        # Bumped whenever a tailed ledger gains rows or resets. The FIFO
        # replay behind get_stats/get_equity_curve is pure in the fills it
        # reads, so this version plus the (net, hedge) it was computed
        # against is a complete cache key: same inputs, same numbers.
        self._fills_version = 0
        self._all_fills_cache: tuple[int, list[Fill]] | None = None
        self._stats_cache: tuple[tuple, Stats] | None = None
        self._curve_cache: tuple[tuple, list[dict]] | None = None
        # Session logs, keyed by path and invalidated on the file's own stat
        # identity: appended once per bot shutdown, so re-parsing per poll
        # would be pure waste.
        self._sessions_cache: dict[Path, tuple[tuple[int, int], list]] = {}
        # Markout rows, tailed like the fills and bounded the same way.
        self._markouts: list[dict] = []
        self._markout_offset: dict[Path, int] = {}
        self._markout_inode: dict[Path, int | None] = {}
        # Symbols seen per venue across the tailed ledgers — decides whether
        # markout rows (which carry no symbol) need joining by trade_id.
        self._venue_symbols: set[str] = set()

    @_synchronized
    def _read_viz(self) -> dict | None:
        """The bot's state file, re-parsed only when it actually changed.

        One /snapshot fans out into ~25 of these calls (orderbook, positions,
        quotes, source_ts and the net/hedge lookup, times every discovered
        bot), and each was re-opening and re-parsing the same JSON. The stat
        identity below — mtime_ns + size + inode — changes on every atomic
        rewrite the bot performs, so a cache hit means the bytes are provably
        the same ones we already parsed, not merely "recent enough".
        """
        try:
            st = self._viz_path.stat()
        except OSError:
            return None
        ident = (st.st_mtime_ns, st.st_size, st.st_ino)
        cached = self._viz_cache
        if cached is not None and cached[0] == ident:
            return cached[1]
        try:
            with open(self._viz_path, "r") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            # OSError: file missing/being rewritten. JSONDecodeError: caught
            # mid atomic-write race (rare, self-heals next poll). Not cached:
            # a torn read must be retried, not remembered.
            return None
        self._viz_cache = (ident, data)
        return data

    @_synchronized
    def get_orderbook(self) -> OrderBook | None:
        data = self._read_viz()
        if not data:
            return None
        ob = data.get("orderbook")
        if not ob:
            return None
        return {
            "bids": [{"price": float(p), "size": float(s)} for p, s in ob.get("bids", [])],
            "asks": [{"price": float(p), "size": float(s)} for p, s in ob.get("asks", [])],
            "best_bid": float(ob.get("best_bid") or 0.0),
            "best_ask": float(ob.get("best_ask") or 0.0),
            "mid": float(ob.get("mid") or 0.0),
            "ts": float(ob.get("ts") or 0.0),
        }

    @_synchronized
    def get_positions(self) -> list[Position]:
        # Sizes here are scaled by `_contract_value`, which is learnt from
        # the fill ledger — so tail it before reading, or the first panel
        # drawn after a restart would print contracts labelled as base.
        self._poll_fills()
        data = self._read_viz()
        if not data:
            return []
        positions: list[Position] = []
        for key in ("position_long", "position_short"):
            p = data.get(key)
            if not p:
                continue
            positions.append(
                {
                    "side": p.get("side", key.split("_")[1].upper()),
                    "qty_base": float(p.get("qty_base") or 0.0) * self._contract_value,
                    "entry_price": p.get("entry_price"),
                    "mark_price": p.get("mark_price"),
                    "unrealised_pnl": p.get("unrealised_pnl"),
                }
            )
        if positions:
            return positions

        # Bots that run a single net book publish `position` instead of the
        # long/short pair. Reported as one side so the panel is identical.
        p = data.get("position")
        if isinstance(p, dict):
            qty = (
                float(p.get("net_position_base") or p.get("qty_base") or 0.0)
                * self._contract_value
            )
            if qty:
                positions.append(
                    {
                        "side": "LONG" if qty > 0 else "SHORT",
                        "qty_base": abs(qty),
                        "entry_price": p.get("entry_price"),
                        "mark_price": p.get("mark_price"),
                        "unrealised_pnl": p.get("unrealised_pnl"),
                    }
                )
        return positions

    @_synchronized
    def get_quotes(self) -> list[Quote]:
        """The bot's own resting orders.

        Key name and row shape vary across bot versions, so several are
        accepted and each row is read defensively. A bot that publishes none
        yields [] and the chart simply draws no quote lines — never an error.
        """
        data = self._read_viz()
        if not data:
            return []
        raw = None
        for key in ("open_orders", "quotes", "orders", "resting_orders"):
            if isinstance(data.get(key), list):
                raw = data[key]
                break
        if raw is None:
            return []

        quotes: list[Quote] = []
        for row in raw:
            try:
                if isinstance(row, dict):
                    # A shared ledger can carry other symbols; skip them.
                    if row.get("symbol") and row["symbol"] != self.symbol:
                        continue
                    side = str(row.get("side") or "").lower()
                    price = float(row.get("price") or 0.0)
                    size = float(row.get("size") or row.get("qty") or 0.0)
                elif isinstance(row, (list, tuple)) and len(row) >= 3:
                    side, price, size = str(row[0]).lower(), float(row[1]), float(row[2])
                else:
                    continue
            except (TypeError, ValueError):
                continue
            if side in ("bid", "b", "long"):
                side = "buy"
            elif side in ("ask", "a", "s", "short"):
                side = "sell"
            if side not in ("buy", "sell") or price <= 0:
                continue
            quotes.append({"side": side, "price": price, "size": size})
        return quotes

    @_synchronized
    def get_source_ts(self) -> float | None:
        """How fresh the *bot's* state is, not how fresh our poll is. The
        orderbook timestamp is the bot's own heartbeat: it only advances
        while the bot is running and writing. Falls back to the payload's
        top-level timestamp, then to the file mtime."""
        data = self._read_viz()
        if data:
            for candidate in ((data.get("orderbook") or {}).get("ts"), data.get("timestamp")):
                if candidate:
                    try:
                        return float(candidate)
                    except (TypeError, ValueError):
                        continue
        try:
            return self._viz_path.stat().st_mtime
        except OSError:
            return None

    @_synchronized
    def get_account(self) -> Account | None:
        # Account/margin comes from a venue REST adapter, not from bot-
        # published files. server.py merges the two at the route level.
        return None

    @_synchronized
    def _net_and_hedge(self) -> tuple[float | None, bool]:
        """The exchange's own position, read fresh from the viz file. This is
        what makes a fills replay checkable: if replaying the ledger lands
        somewhere else, the ledger is missing history and whatever the
        replay claims is fiction. Shared by every replay-based reader
        (`get_stats`, `get_equity_curve`) so they can never disagree about
        what the exchange currently holds.

        Scaled to base units by `_contract_value`, because the field is
        misnamed on at least one venue: the OKX engine publishes
        `net_position_base: 2489` while holding 24.89 SOL (ctVal 0.01).
        Compared raw against a replay that *is* in base units, the check
        could only ever fail — it refused the panel for a reason that had
        nothing to do with the ledger (observed 2026-08-12).
        """
        data = self._read_viz() or {}
        net = None
        pos = data.get("position")
        if isinstance(pos, dict) and pos.get("net_position_base") is not None:
            try:
                net = float(pos["net_position_base"]) * self._contract_value
            except (TypeError, ValueError):
                net = None
        if net is None:
            longs = data.get("position_long") or {}
            shorts = data.get("position_short") or {}
            if longs or shorts:
                net = (
                    float(longs.get("qty_base") or 0.0) - float(shorts.get("qty_base") or 0.0)
                ) * self._contract_value

        hedge = bool(
            float((data.get("position_long") or {}).get("qty_base") or 0.0) > 0
            and float((data.get("position_short") or {}).get("qty_base") or 0.0) > 0
        )
        return net, hedge

    @_synchronized
    def get_sessions(self, observed_start_ts: float | None = None) -> list[Session]:
        """Every recorded session for this bot, plus the running one.

        The session log is re-read only when it changes on disk (it is
        appended to once per bot shutdown, so re-parsing it on every poll
        would be pure waste), and merged across ledgers the same way fills
        are: a bot whose fills come from two trackers has both trackers'
        session logs.
        """
        self._poll_fills()
        recorded: list[tuple[float, float, bool | None]] = []
        for path in self._fills_paths:
            # Only the ledgers this bot actually appears in. Discovery hands
            # every bot every ledger it found (the per-row venue/symbol
            # filter is what sorts them out), so without this a Bitunix bot
            # would also inherit the OKX tracker's session log — observed
            # 2026-08-12: nine sessions instead of five, several of them
            # duplicated, and every window bounded by another bot's run.
            if not self._fills_by_path.get(path):
                continue
            csv_path = sessions_csv_for(path)
            if csv_path is None:
                continue
            try:
                ident = (csv_path.stat().st_mtime_ns, csv_path.stat().st_size)
            except OSError:
                continue
            cached = self._sessions_cache.get(csv_path)
            if cached is None or cached[0] != ident:
                cached = (ident, parse_sessions_csv(csv_path))
                self._sessions_cache[csv_path] = cached
            recorded.extend(cached[1])
        recorded.sort(key=lambda s: s[0])

        last_end = recorded[-1][1] if recorded else None
        first_after = None
        for f in self._all_fills():
            if last_end is None or f["ts"] > last_end:
                first_after = f["ts"]
                break
        return build_sessions(recorded, first_after, observed_start_ts)

    def get_markouts(
        self, since_ts: float | None = None, until_ts: float | None = None
    ) -> dict:
        """Post-fill mid drift for this bot's own fills over a window.

        Two ways to tell this bot's markouts apart from another bot's on the
        same venue, because the tracker's markout rows carry `venue` but no
        `symbol`:

        - Join on `trade_id` against the fills we already hold. Exact, and
          required when the venue carries more than one symbol (the Bitunix
          ledger also holds ETHUSDT). Joins 100% on Bitunix.
        - Fall back to venue + time window when the venue's ledger holds a
          single symbol, where there is nothing to disambiguate. Needed for
          Coinbase, whose tracker aggregates fills per order while markouts
          are per execution, so the ids genuinely don't correspond (4% join).

        Never blends the two: a partial join would silently drop 96% of the
        Coinbase sample and report the remainder as if it were the whole.
        """
        self._poll_fills()
        self._poll_markouts()
        rows = [
            r
            for r in self._markouts
            if (since_ts is None or r.get("fill_ts", 0.0) >= since_ts)
            and (until_ts is None or r.get("fill_ts", 0.0) <= until_ts)
        ]
        joined_by = "venue+window"
        if self._venue_is_multi_symbol():
            ids = {f.get("trade_id") for f in self._all_fills() if f.get("trade_id")}
            rows = [r for r in rows if str(r.get("trade_id") or "") in ids]
            joined_by = "trade_id"
        return {
            "points": aggregate(rows),
            "n_fills": len(rows),
            "joined_by": joined_by,
        }

    @_synchronized
    def _venue_is_multi_symbol(self) -> bool:
        """Whether this bot's venue carries more than one symbol in the
        ledgers being tailed — i.e. whether markout rows for this venue could
        belong to a different bot."""
        return len(self._venue_symbols) > 1

    @_synchronized
    def _poll_markouts(self) -> None:
        for path in self._fills_paths:
            mo_path = markouts_path_for(path)
            if mo_path is None:
                continue
            try:
                st = mo_path.stat()
            except OSError:
                continue
            offset = self._markout_offset.get(mo_path, 0)
            if self._markout_inode.get(mo_path) is not None and (
                st.st_ino != self._markout_inode[mo_path] or st.st_size < offset
            ):
                offset = 0
                self._markouts = [m for m in self._markouts if m.get("_src") != str(mo_path)]
            self._markout_inode[mo_path] = st.st_ino
            if st.st_size == offset:
                continue
            try:
                with open(mo_path, "r") as f:
                    f.seek(offset)
                    data = f.read()
                    offset = f.tell()
            except OSError:
                continue
            self._markout_offset[mo_path] = offset
            for line in data.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("venue") and row["venue"] != self.venue_name:
                    continue
                row["_src"] = str(mo_path)
                self._markouts.append(row)
            # Bounded like the fill window, for the same reason.
            if len(self._markouts) > self._fills_maxlen:
                self._markouts = self._markouts[-self._fills_maxlen :]

    @_synchronized
    def _opening_inventory_recorded(self, since_ts: float) -> float | None:
        """What the bot itself said it was holding when the session's first
        fill landed. None when the ledger doesn't record inventory, or when
        the session has no fills yet — in both cases there is nothing to
        check the replay against, and `_reliability` is told so rather than
        being handed a zero it would read as "verified flat"."""
        for f in self._all_fills():
            if f["ts"] >= since_ts:
                return f.get("inventory_before")
        return None

    @_synchronized
    def get_stats(self, since_ts: float | None = None, until_ts: float | None = None) -> Stats:
        self._poll_fills()
        net, hedge = self._net_and_hedge()
        # Memoised on the exact inputs the replay consumes, so a hit returns
        # figures identical to recomputing — not an approximation and not a
        # time-based "probably still fine". /snapshot calls this once per
        # discovered bot plus once for the selected one, and the replay walks
        # the whole fills window every time; without this the same arithmetic
        # ran five times per request over unchanged data.
        opening = self._opening_inventory_recorded(since_ts) if since_ts is not None else None
        key = (self._fills_version, net, hedge, since_ts, until_ts, opening)
        cached = self._stats_cache
        if cached is not None and cached[0] == key:
            return cached[1]
        stats = compute_stats(
            self._all_fills(),
            net_position_base=net,
            hedge_mode=hedge,
            since_ts=since_ts,
            until_ts=until_ts,
            opening_inventory_recorded=opening,
        )
        self._stats_cache = (key, stats)
        return stats

    def get_equity_curve(
        self, since_ts: float | None = None, until_ts: float | None = None
    ) -> list[dict]:
        """Cumulative realised PnL and running inventory, one point per
        fill — the same replay `get_stats` runs, kept as a series instead of
        collapsed to a total. See `adapters/stats.py:equity_curve` for what
        each point means and why `pnl_unreliable` rides along on all of
        them."""
        self._poll_fills()
        net, hedge = self._net_and_hedge()
        opening = self._opening_inventory_recorded(since_ts) if since_ts is not None else None
        key = (self._fills_version, net, hedge, since_ts, until_ts, opening)  # as get_stats
        cached = self._curve_cache
        if cached is not None and cached[0] == key:
            return cached[1]
        curve = equity_curve(
            self._all_fills(),
            net_position_base=net,
            hedge_mode=hedge,
            since_ts=since_ts,
            until_ts=until_ts,
            opening_inventory_recorded=opening,
        )
        self._curve_cache = (key, curve)
        return curve

    @_synchronized
    def get_first_fill_ts(self) -> float | None:
        """When the bot's recorded activity starts, as far as this window
        knows. Used to decide how much chart history to pull: an operator
        wants to see the session, not a fixed number of bars.

        It is the start of the *window*, not of the bot's life — the ledger
        is capped — so the chart reaches back as far as the fills do and no
        further, which is the honest boundary anyway."""
        self._poll_fills()
        fills = self._all_fills()
        if not fills:
            return None
        # _all_fills is sorted by ts, so the first element *is* the minimum —
        # scanning all of them with min() was O(n) over the whole window on
        # every request for a value the sort had already determined.
        return fills[0]["ts"]

    @_synchronized
    def get_recent_fills(self, limit: int = 500) -> list[Fill]:
        self._poll_fills()
        return self._all_fills()[-limit:]

    @_synchronized
    def _all_fills(self) -> list[Fill]:
        """Every matching fill across every tailed ledger, merged into one
        chronological list. Each source file is individually append-only and
        chronological, but two files are tailed independently, so a merge +
        sort is what keeps the combined view in order — a plain concat would
        only be safe by coincidence.

        Cached on the fills version: the merge+sort is O(n log n) over the
        whole window and every caller in one /snapshot asks for the same
        unchanged list."""
        cached = self._all_fills_cache
        if cached is not None and cached[0] == self._fills_version:
            return cached[1]
        merged: list[Fill] = []
        for fills in self._fills_by_path.values():
            merged.extend(fills)
        merged.sort(key=lambda f: f["ts"])
        if len(merged) > self._fills_maxlen:
            merged = merged[-self._fills_maxlen :]
        self._all_fills_cache = (self._fills_version, merged)
        return merged

    @_synchronized
    def _poll_fills(self) -> None:
        for path in self._fills_paths:
            self._poll_one_fills_file(path)

    @_synchronized
    def _poll_one_fills_file(self, path: Path) -> None:
        try:
            st = path.stat()
        except OSError:
            return

        offset = self._fills_offset[path]
        # File replaced/truncated (e.g. this ledger reset) -> re-read from
        # start. Scoped to this path only — another tailed ledger's fills
        # are untouched.
        if self._fills_inode[path] is not None and (
            st.st_ino != self._fills_inode[path] or st.st_size < offset
        ):
            offset = 0
            self._fills_by_path[path].clear()
            # A reset drops rows, which changes the replay just as much as an
            # append does — the caches keyed on this version must not survive
            # it.
            self._fills_version += 1
        self._fills_inode[path] = st.st_ino

        if st.st_size == offset:
            self._fills_offset[path] = offset
            return

        try:
            with open(path, "r") as f:
                f.seek(offset)
                new_data = f.read()
                offset = f.tell()
        except OSError:
            return
        self._fills_offset[path] = offset

        for line in new_data.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            # One ledger can serve more than one bot, so filter on both
            # axes. Filtering by symbol alone would mix two venues trading
            # the same pair into a single PnL.
            venue = row.get("venue")
            if venue and venue != self.venue_name:
                continue
            # Tracked before the symbol filter: this is what tells whether
            # this venue carries more than one bot's flow.
            if row.get("symbol"):
                self._venue_symbols.add(str(row["symbol"]))
            if row.get("symbol") != self.symbol:
                continue
            cv = _opt_float(row.get("contract_value"))
            if cv is not None and cv > 0:
                self._contract_value = cv
            self._fills_by_path[path].append(
                {
                    "ts": float(row.get("ts") or 0.0),
                    "side": str(row.get("side") or ""),
                    "price": float(row.get("price") or 0.0),
                    "size": float(row.get("size") or 0.0),
                    # Kept because for a maker strategy the fee is often the
                    # difference between a positive and a negative result —
                    # a gross-only PnL would flatter every window.
                    #
                    # Normalised to a positive cost, because the sign
                    # convention is not the same across this fleet's
                    # trackers: Bitunix and Coinbase rows record a charged
                    # fee as positive, while OKX rows carry the venue's own
                    # convention where a *charge* is negative and a rebate
                    # positive. `realised_net = realised - fees` therefore
                    # added OKX's fees to the PnL instead of subtracting
                    # them: on 2026-08-12 that turned a real -0.80 into a
                    # displayed +5.20 over 931 fills (gross +2.1978, fees
                    # 2.9975 = 2.35bps of 12779 quote volume, i.e. exactly
                    # the standard maker charge, not a rebate).
                    #
                    # Taking the magnitude is the honest floor: a fee is a
                    # cost, and this dashboard would rather understate a
                    # rebate than silently book a charge as income. A venue
                    # that genuinely pays a rebate needs its own signed
                    # handling here, declared per venue, not an inferred sign.
                    "fee": abs(float(row.get("fee") or 0.0)),
                    # The bot's own inventory reading just before this fill,
                    # when it records one (Coinbase's tracker writes null).
                    # This is what lets a session's rebuilt opening position
                    # be *checked* instead of trusted — see the opening gap
                    # in adapters/stats.py.
                    "inventory_before": _opt_float(row.get("inventory_before")),
                    # Position counter and process id from the engine itself.
                    # Consecutive rows of one run must differ by exactly 1;
                    # anything else is a fill the recorder never captured.
                    # This is the only *positive* evidence of an incomplete
                    # ledger available here — the inventory checks in
                    # adapters/stats.py infer it, this one proves it. None on
                    # rows written before the engine emitted the counter.
                    "fill_seq": _opt_float(row.get("fill_seq")),
                    "fill_run_id": _opt_float(row.get("fill_run_id")),
                    # The venue mid at the moment of the fill. Used to mark
                    # a session's opening and closing inventory for the
                    # cash-flow PnL — a better mark than the fill price,
                    # which sits a half-spread away from it by construction.
                    "mid_at_fill": _opt_float(row.get("mid_at_fill")),
                    # Kept solely to join this fill to its markout row: the
                    # markout log carries no symbol, so on a venue running
                    # more than one bot this id is the only thing that says
                    # which flow a markout belongs to.
                    "trade_id": str(row.get("trade_id") or ""),
                }
            )
            self._fills_version += 1
