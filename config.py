"""Single place that knows about paths, symbols, and which adapters are active.

To add a venue later: write its adapter module, add one entry to VENUES,
nothing else in the repo changes.
"""

from __future__ import annotations

import os
from pathlib import Path

from adapters.bitunix_account import BitunixAccountAdapter
from adapters.bitunix_files import BitunixFileAdapter
from adapters.bitunix_klines import BitunixKlineAdapter

# Where the v17mm bot (read-only) publishes its state. Overridable via env
# so the dashboard can run against a different bot install without a code
# change.
BITUNIX_SYMBOL = os.environ.get("BITUNIX_SYMBOL", "SOLUSDT")
BITUNIX_VIZ_PATH = Path(
    os.environ.get("BITUNIX_VIZ_PATH", "/root/bots/v17mm/bitunix_mm/.v15mm_hl_viz.json")
)
BITUNIX_FILLS_PATH = Path(
    os.environ.get("BITUNIX_FILLS_PATH", "/root/.v17mm_tracker/fills.jsonl")
)

POLL_INTERVAL_S = float(os.environ.get("POLL_INTERVAL_S", "1.0"))
ACCOUNT_POLL_INTERVAL_S = float(os.environ.get("ACCOUNT_POLL_INTERVAL_S", "5.0"))
KLINE_POLL_INTERVAL_S = float(os.environ.get("KLINE_POLL_INTERVAL_S", "20.0"))

BIND_HOST = os.environ.get("BIND_HOST", "127.0.0.1")
BIND_PORT = int(os.environ.get("BIND_PORT", "8091"))


def build_bitunix_file_adapter() -> BitunixFileAdapter:
    return BitunixFileAdapter(
        symbol=BITUNIX_SYMBOL,
        viz_path=BITUNIX_VIZ_PATH,
        fills_path=BITUNIX_FILLS_PATH,
    )


def build_bitunix_account_adapter() -> BitunixAccountAdapter | None:
    api_key = os.environ.get("BITUNIX_API_KEY")
    secret_key = os.environ.get("BITUNIX_SECRET_KEY")
    if not api_key or not secret_key:
        return None
    return BitunixAccountAdapter(
        api_key=api_key,
        secret_key=secret_key,
        poll_interval_s=ACCOUNT_POLL_INTERVAL_S,
    )


def build_bitunix_kline_adapter() -> BitunixKlineAdapter:
    return BitunixKlineAdapter(
        symbol=BITUNIX_SYMBOL,
        poll_interval_s=KLINE_POLL_INTERVAL_S,
    )


# v1: exactly one venue, one bot, one book. Registered here so server.py
# never hardcodes "bitunix" anywhere in its own logic.
VENUES = {
    "bitunix": {
        "file_adapter": build_bitunix_file_adapter,
        "account_adapter": build_bitunix_account_adapter,
        "kline_adapter": build_bitunix_kline_adapter,
    }
}
