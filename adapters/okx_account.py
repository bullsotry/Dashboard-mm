"""OKX adapter, account half: margin/equity via OKX's own REST API.

Same posture as the Bitunix account adapter: talks directly to OKX
(https://www.okx.com), not to the bot, with its own credentials, GET-only,
returns None on any failure or missing key so the dashboard just omits the
margin panel rather than erroring the whole snapshot.

The signing scheme (OK-ACCESS-KEY/SIGN/TIMESTAMP/PASSPHRASE, HMAC-SHA256
base64 over timestamp+method+path+body, millisecond-precision ISO-8601
timestamp) is OKX's documented v5 auth, ported from v17mm OKX's own
okx_client.py — that client's signing has been running against this same
account since 2026-08-05, so this is a known-working scheme, not a fresh
implementation guessed from docs alone.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from datetime import datetime, timezone

import requests

from .base import Account

_BASE_URL = "https://www.okx.com"
_BALANCE_PATH = "/api/v5/account/balance"


def _iso_timestamp() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _sign(secret_key: str, timestamp: str, method: str, request_path: str, body: str) -> str:
    prehash = f"{timestamp}{method.upper()}{request_path}{body}"
    digest = hmac.new(secret_key.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


class OkxAccountAdapter:
    def __init__(
        self,
        api_key: str,
        secret_key: str,
        passphrase: str,
        margin_coin: str = "USDC",
        poll_interval_s: float = 5.0,
    ):
        self._api_key = api_key
        self._secret_key = secret_key
        self._passphrase = passphrase
        self._margin_coin = margin_coin
        self._poll_interval_s = poll_interval_s
        self._cached: Account | None = None
        self._last_poll_ts = 0.0

    def get_account(self) -> Account | None:
        now = time.time()
        if now - self._last_poll_ts < self._poll_interval_s:
            return self._cached
        self._last_poll_ts = now

        query = f"?ccy={self._margin_coin}"
        request_path = _BALANCE_PATH + query
        timestamp = _iso_timestamp()
        sign = _sign(self._secret_key, timestamp, "GET", request_path, "")
        headers = {
            "OK-ACCESS-KEY": self._api_key,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self._passphrase,
            "Content-Type": "application/json",
        }
        try:
            resp = requests.get(_BASE_URL + request_path, headers=headers, timeout=5.0)
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError):
            return self._cached  # keep last-known-good rather than blanking the panel

        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not rows:
            return self._cached
        details = [d for d in (rows[0].get("details") or []) if isinstance(d, dict)]
        detail = next((d for d in details if str(d.get("ccy") or "").upper() == self._margin_coin), None)
        if detail is None:
            detail = details[0] if details else {}

        def _f(*keys: str) -> float:
            for k in keys:
                v = detail.get(k)
                if v not in (None, ""):
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        continue
            return 0.0

        equity = _f("eq")
        available = _f("availEq", "availBal", "cashBal")
        upnl = _f("upl")
        # Residual = whatever's locked up backing open positions, including
        # any uPnL not already reflected in availEq. Mirrors the "margin"
        # field's meaning on the Bitunix adapter: not a venue-native field,
        # a derived one, so the two panels read the same way.
        margin_used = max(0.0, equity - available - upnl)

        self._cached = {
            "available": available,
            "margin_used": margin_used,
            "unrealised_pnl": upnl,
        }
        return self._cached
