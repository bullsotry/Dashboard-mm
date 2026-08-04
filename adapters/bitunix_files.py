"""Bitunix adapter, file half: orderbook + positions + fills.

Reads two files the v17mm bot (and its separate tracker service) already
publish. Never writes to them, never imports anything from the bot's code,
never touches the bot's process. If a file is missing or briefly invalid
(mid-write), methods return None / [] rather than raising — a monitoring
dashboard must degrade quietly, not crash a poll loop.
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path

from .base import Account, Fill, OrderBook, Position


class BitunixFileAdapter:
    venue_name = "bitunix"

    def __init__(self, symbol: str, viz_path: Path, fills_path: Path, fills_maxlen: int = 500):
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
        except (OSError, json.JSONDecodeError):
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
        return positions

    def get_account(self) -> Account | None:
        # Account/margin comes from BitunixAccountAdapter (live REST poll),
        # not from bot-published files. This adapter never reports account
        # state — server.py merges the two Bitunix adapters at the route level.
        return None

    def get_recent_fills(self, limit: int = 200) -> list[Fill]:
        self._poll_fills()
        return list(self._fills)[-limit:]

    def _poll_fills(self) -> None:
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
            if row.get("symbol") != self.symbol:
                continue
            self._fills.append(
                {
                    "ts": float(row.get("ts") or 0.0),
                    "side": str(row.get("side") or ""),
                    "price": float(row.get("price") or 0.0),
                    "size": float(row.get("size") or 0.0),
                }
            )
