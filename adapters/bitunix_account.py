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
        try:
            available = float(item.get("available") or 0.0)
            margin_used = float(item.get("margin") or 0.0)
            cross_upnl = float(item.get("crossUnrealizedPNL") or 0.0)
        except (TypeError, ValueError):
            return self._cached

        self._cached = {
            "available": available,
            "margin_used": margin_used,
            "unrealised_pnl": cross_upnl,
            # Bitunix doesn't expose a single equity field (unlike OKX's
            # "eq") — derived the same way OKX's own margin_used is derived,
            # so the two venues' NAV panels agree on what the number means.
            "equity": available + margin_used + cross_upnl,
        }
        return self._cached
