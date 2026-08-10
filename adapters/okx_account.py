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

The request itself is *not* sent through `requests`/urllib3, for the same
reason the bot's own client isn't: OKX enforces its IP allow-list against a
specific IPv4 address, but on a dual-stack host plain HTTPSConnection lets
the OS pick IPv6, which OKX then rejects with a misleading "API key doesn't
exist" (50119) even though the IPv4 address is correctly whitelisted —
confirmed live on this dashboard's own Tokyo VPS. `_IPv4HTTPSConnection` is
the same fix v17mm OKX's okx_client.py uses, ported here rather than
imported so this adapter has no dependency on the bot's codebase.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import socket
import ssl
import time
from datetime import datetime, timezone

from .base import Account

_HOST = "www.okx.com"
_BALANCE_PATH = "/api/v5/account/balance"


def _iso_timestamp() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _sign(secret_key: str, timestamp: str, method: str, request_path: str, body: str) -> str:
    prehash = f"{timestamp}{method.upper()}{request_path}{body}"
    digest = hmac.new(secret_key.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


class _IPv4HTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection that resolves and connects using AF_INET only. See
    module docstring — this is what makes OKX's IP allow-list actually see
    the whitelisted address on a dual-stack host."""

    def connect(self):
        err: OSError | None = None
        for family, socktype, proto, _, sockaddr in socket.getaddrinfo(
            self.host, self.port, socket.AF_INET, socket.SOCK_STREAM
        ):
            try:
                sock = socket.socket(family, socktype, proto)
                sock.settimeout(self.timeout)
                sock.connect(sockaddr)
                self.sock = sock
                break
            except OSError as e:
                err = e
                continue
        else:
            raise OSError(f"IPv4 connection to {self.host}:{self.port} failed") from err
        if self._tunnel_host:
            self._tunnel()
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


def _get_ipv4(path: str, headers: dict, timeout_s: float) -> tuple[int, bytes]:
    conn = _IPv4HTTPSConnection(_HOST, 443, timeout=timeout_s, context=ssl.create_default_context())
    try:
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


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
            status, body = _get_ipv4(request_path, headers, timeout_s=5.0)
            if status != 200:
                return self._cached
            payload = json.loads(body)
        except (OSError, ValueError):
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
            "equity": equity,
        }
        return self._cached
