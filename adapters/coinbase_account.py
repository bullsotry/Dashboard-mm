"""Coinbase adapter, account half: quote-currency balance via the official
Advanced Trade SDK.

Unlike Bitunix/OKX this doesn't hand-roll the request: Coinbase's current
key type (CDP) signs with an ES256 JWT over an EC private key, not a simple
HMAC, and the `coinbase-advanced-py` SDK the bot itself already depends on
is the maintained, correct implementation of that — reimplementing JWT
signing by hand here would be new, untested crypto for no benefit.

Coinbase is spot, not margin, so this panel is a narrower analog of the
Bitunix/OKX one: `available` is the quote-currency balance actually free to
trade with, `margin_used` is what's held by open orders (spot's nearest
equivalent to "locked"), and `unrealised_pnl` is always 0.0 — a spot
position's mark-to-market shows up as inventory value, not account PnL, so
there is nothing this field can honestly report.
"""

from __future__ import annotations

import time

from .base import Account

try:
    from coinbase.rest import RESTClient
except ImportError:  # optional dependency; account panel just stays off
    RESTClient = None


def _quote_currency(symbol: str) -> str:
    """"SOL-USDC" -> "USDC". Falls back to the whole symbol if there's no
    separator — better to look for a currency that doesn't exist (empty
    panel) than to silently pick the wrong one."""
    return symbol.split("-", 1)[1] if "-" in symbol else symbol


class CoinbaseAccountAdapter:
    def __init__(self, api_key: str, api_secret: str, symbol: str, poll_interval_s: float = 5.0):
        self._client = RESTClient(api_key=api_key, api_secret=api_secret) if RESTClient else None
        self._quote = _quote_currency(symbol)
        self._poll_interval_s = poll_interval_s
        self._cached: Account | None = None
        self._last_poll_ts = 0.0

    def get_account(self) -> Account | None:
        if self._client is None:
            return None
        now = time.time()
        if now - self._last_poll_ts < self._poll_interval_s:
            return self._cached
        self._last_poll_ts = now

        try:
            resp = self._client.get_accounts(limit=250)
        except Exception:
            return self._cached  # keep last-known-good rather than blanking the panel

        accounts = getattr(resp, "accounts", None) or []
        account = next((a for a in accounts if getattr(a, "currency", None) == self._quote), None)
        if account is None:
            return self._cached

        def _amount(obj) -> float:
            if obj is None:
                return 0.0
            val = getattr(obj, "value", None) if not isinstance(obj, dict) else obj.get("value")
            try:
                return float(val or 0.0)
            except (TypeError, ValueError):
                return 0.0

        available = _amount(getattr(account, "available_balance", None))
        held = _amount(getattr(account, "hold", None))

        self._cached = {
            "available": available,
            "margin_used": held,
            "unrealised_pnl": 0.0,
        }
        return self._cached
