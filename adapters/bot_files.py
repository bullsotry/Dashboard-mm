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

import json
from collections import deque
from pathlib import Path

from .base import Account, Fill, OrderBook, Position, Quote, Stats
from .stats import compute_stats, equity_curve


class BotStateAdapter:
    def __init__(
        self,
        venue_name: str,
        symbol: str,
        viz_path: Path,
        fills_path: Path | None,
        fills_maxlen: int = 2000,
    ):
        self.venue_name = venue_name
        self.symbol = symbol
        self._viz_path = viz_path
        self._fills_path = fills_path
        self._fills: deque[Fill] = deque(maxlen=fills_maxlen)
        self._fills_offset = 0
        self._fills_inode: int | None = None

    def _read_viz(self) -> dict | None:
        try:
            with open(self._viz_path, "r") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            # OSError: file missing/being rewritten. JSONDecodeError: caught
            # mid atomic-write race (rare, self-heals next poll).
            return None

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

    def get_positions(self) -> list[Position]:
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
                    "qty_base": float(p.get("qty_base") or 0.0),
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
            qty = float(p.get("net_position_base") or p.get("qty_base") or 0.0)
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

    def get_account(self) -> Account | None:
        # Account/margin comes from a venue REST adapter, not from bot-
        # published files. server.py merges the two at the route level.
        return None

    def _net_and_hedge(self) -> tuple[float | None, bool]:
        """The exchange's own position, read fresh from the viz file. This is
        what makes a fills replay checkable: if replaying the ledger lands
        somewhere else, the ledger is missing history and whatever the
        replay claims is fiction. Shared by every replay-based reader
        (`get_stats`, `get_equity_curve`) so they can never disagree about
        what the exchange currently holds.
        """
        data = self._read_viz() or {}
        net = None
        pos = data.get("position")
        if isinstance(pos, dict) and pos.get("net_position_base") is not None:
            try:
                net = float(pos["net_position_base"])
            except (TypeError, ValueError):
                net = None
        if net is None:
            longs = data.get("position_long") or {}
            shorts = data.get("position_short") or {}
            if longs or shorts:
                net = float(longs.get("qty_base") or 0.0) - float(shorts.get("qty_base") or 0.0)

        hedge = bool(
            float((data.get("position_long") or {}).get("qty_base") or 0.0) > 0
            and float((data.get("position_short") or {}).get("qty_base") or 0.0) > 0
        )
        return net, hedge

    def get_stats(self) -> Stats:
        self._poll_fills()
        net, hedge = self._net_and_hedge()
        return compute_stats(list(self._fills), net_position_base=net, hedge_mode=hedge)

    def get_equity_curve(self) -> list[dict]:
        """Cumulative realised PnL and running inventory, one point per
        fill — the same replay `get_stats` runs, kept as a series instead of
        collapsed to a total. See `adapters/stats.py:equity_curve` for what
        each point means and why `pnl_unreliable` rides along on all of
        them."""
        self._poll_fills()
        net, hedge = self._net_and_hedge()
        return equity_curve(list(self._fills), net_position_base=net, hedge_mode=hedge)

    def get_first_fill_ts(self) -> float | None:
        """When the bot's recorded activity starts, as far as this window
        knows. Used to decide how much chart history to pull: an operator
        wants to see the session, not a fixed number of bars.

        It is the start of the *window*, not of the bot's life — the ledger
        is capped — so the chart reaches back as far as the fills do and no
        further, which is the honest boundary anyway."""
        self._poll_fills()
        if not self._fills:
            return None
        return min(f["ts"] for f in self._fills)

    def get_recent_fills(self, limit: int = 500) -> list[Fill]:
        self._poll_fills()
        return list(self._fills)[-limit:]

    def _poll_fills(self) -> None:
        if self._fills_path is None:
            return
        try:
            st = self._fills_path.stat()
        except OSError:
            return

        # File replaced/truncated (e.g. ledger reset) -> re-read from start.
        if self._fills_inode is not None and (
            st.st_ino != self._fills_inode or st.st_size < self._fills_offset
        ):
            self._fills_offset = 0
            self._fills.clear()
        self._fills_inode = st.st_ino

        if st.st_size == self._fills_offset:
            return

        try:
            with open(self._fills_path, "r") as f:
                f.seek(self._fills_offset)
                new_data = f.read()
                self._fills_offset = f.tell()
        except OSError:
            return

        for line in new_data.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            # One ledger serves the whole fleet, so filter on both axes.
            # Filtering by symbol alone would mix two venues trading the
            # same pair into a single PnL.
            if row.get("symbol") != self.symbol:
                continue
            venue = row.get("venue")
            if venue and venue != self.venue_name:
                continue
            self._fills.append(
                {
                    "ts": float(row.get("ts") or 0.0),
                    "side": str(row.get("side") or ""),
                    "price": float(row.get("price") or 0.0),
                    "size": float(row.get("size") or 0.0),
                    # Kept because for a maker strategy the fee is often the
                    # difference between a positive and a negative result —
                    # a gross-only PnL would flatter every window.
                    "fee": float(row.get("fee") or 0.0),
                }
            )
