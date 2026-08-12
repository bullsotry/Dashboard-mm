"""Bitunix adapter, account half: margin/equity via Bitunix's own REST API.

This talks directly to Bitunix (https://fapi.bitunix.com), not to the bot.
It uses its own read-only API key (trade/withdraw unchecked) so it is fully
isolated from the trading key — the auth scheme below is Bitunix's public,
documented signing method (SHA-256 double-hash), independent of any bot code.

GET-only. If credentials are missing or Bitunix errors out, get_account()
returns None and the dashboard just omits the margin panel for that poll.
"""

from __future__ import annotations

import hashlib
import time
import uuid

import requests

from .base import Account

_BASE_URL = "https://fapi.bitunix.com"
_ACCOUNT_PATH = "/api/v1/futures/account"


def _sign(api_key: str, secret_key: str, nonce: str, timestamp: str, query_str: str) -> str:
    digest = hashlib.sha256(
        (nonce + timestamp + api_key + query_str + "").encode("utf-8")
    ).hexdigest()
    return hashlib.sha256((digest + secret_key).encode("utf-8")).hexdigest()


class BitunixAccountAdapter:
    """Polls account-level margin/equity. Not part of VenueAdapter's file
    read path — server.py merges this with BitunixFileAdapter's output."""

    def __init__(self, api_key: str, secret_key: str, margin_coin: str = "USDT", poll_interval_s: float = 5.0):
        self._api_key = api_key
        self._secret_key = secret_key
        self._margin_coin = margin_coin
        self._poll_interval_s = poll_interval_s
        self._cached: Account | None = None
        self._last_poll_ts = 0.0

    def get_account(self) -> Account | None:
        now = time.time()
        if now - self._last_poll_ts < self._poll_interval_s:
            return self._cached
        self._last_poll_ts = now

        params = {"marginCoin": self._margin_coin}
        query_str = "".join(f"{k}{v}" for k, v in sorted(params.items()))
        nonce = uuid.uuid4().hex
        timestamp = str(int(now * 1000))
        sign = _sign(self._api_key, self._secret_key, nonce, timestamp, query_str)
        headers = {
            "api-key": self._api_key,
            "nonce": nonce,
            "timestamp": timestamp,
            "sign": sign,
            "language": "en-US",
        }
        try:
            resp = requests.get(
                _BASE_URL + _ACCOUNT_PATH, params=params, headers=headers, timeout=5.0
            )
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError):
            return self._cached  # keep last-known-good rather than blanking the panel

        data = payload.get("data") if isinstance(payload, dict) else None
        items = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
        if not items:
            return self._cached

        item = items[0]

        def _f(key: str) -> float:
            try:
                return float(item.get(key) or 0.0)
            except (TypeError, ValueError):
                return 0.0

        available = _f("available")
        margin_used = _f("margin")
        # Both halves, not just the cross one. This account runs its
        # positions in isolated mode, so `crossUnrealizedPNL` reads 0.00
        # while `isolationUnrealizedPNL` carries the actual figure — reading
        # only the first reported "no unrealised PnL" on a bot that had some.
        upnl = _f("crossUnrealizedPNL") + _f("isolationUnrealizedPNL")
        # The missing 87%: measured live 2026-08-12, `frozen` held 92.43 USDT
        # against an `available` of 1.73 and a `margin` of 12.68. Omitting it
        # reported a 14.42 NAV on a 106.95 account, and made that NAV lurch
        # every time the bot placed or pulled a quote.
        frozen = _f("frozen")

        self._cached = {
            "available": available,
            "margin_used": margin_used,
            "frozen": frozen,
            "unrealised_pnl": upnl,
            # `bonus` is spendable margin the venue granted; `transfer` is
            # deliberately excluded — it mirrors `available` (both read
            # 1.7342580352669438 live) and adding it would double-count.
            "equity": available + frozen + margin_used + upnl + _f("bonus"),
            "equity_scope": f"{self._margin_coin} futures account",
            "account_equity_total": None,
        }
        return self._cached
