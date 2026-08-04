"""Venue adapter contract.

Every venue (Bitunix today, Hyperliquid later) implements this Protocol.
server.py and the frontend never import a concrete adapter directly by
path/behavior — they only call these four methods. Adding a venue means
writing one new adapter module and registering it in config.py; nothing
else in this repo changes.
"""

from __future__ import annotations

from typing import Any, Protocol, TypedDict


class OrderBookLevel(TypedDict):
    price: float
    size: float


class OrderBook(TypedDict):
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]
    best_bid: float
    best_ask: float
    mid: float
    ts: float


class Fill(TypedDict):
    ts: float
    side: str  # "buy" | "sell"
    price: float
    size: float


class Position(TypedDict):
    side: str  # "LONG" | "SHORT"
    qty_base: float
    entry_price: float | None
    mark_price: float | None
    unrealised_pnl: float | None


class Account(TypedDict):
    available: float
    margin_used: float
    unrealised_pnl: float


class Candle(TypedDict):
    time: int  # unix seconds, candle open time
    open: float
    high: float
    low: float
    close: float


class VenueAdapter(Protocol):
    symbol: str
    venue_name: str

    def get_orderbook(self) -> OrderBook | None: ...

    def get_recent_fills(self, limit: int = 200) -> list[Fill]: ...

    def get_positions(self) -> list[Position]: ...

    def get_account(self) -> Account | None: ...

    def get_klines(self) -> list[Candle]: ...
