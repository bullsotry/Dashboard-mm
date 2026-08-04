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
    fee: float  # quote currency, as reported by the venue; 0.0 if absent


class Quote(TypedDict):
    """A resting order belonging to the bot — not a market book level."""

    side: str  # "buy" | "sell"
    price: float
    size: float


class Stats(TypedDict):
    """Derived from the fills window only. See adapters/stats.py for the
    definitions and their limits — every field here is a measurement over a
    bounded window, never an all-time figure."""

    span_s: float  # seconds actually covered by the fills used
    n_fills: int
    n_buys: int
    n_sells: int
    volume_quote: float
    fills_per_hour: float
    realised_gross: float  # FIFO, fees excluded
    fees: float
    realised_net: float  # realised_gross - fees
    inventory_base: float  # net signed base accumulated over the window
    capture_bps: float | None  # avg sell vs avg buy, None if one side is empty


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

    def get_quotes(self) -> list[Quote]: ...

    def get_source_ts(self) -> float | None: ...
    """Age of the venue's own published state, independent of whether this
    dashboard's poll loop is healthy. A live server serving a dead bot's
    stale file is the failure mode this exists to expose."""
