"""Where to look for bots, and which venue-specific adapters exist.

There is deliberately no list of bots here. Bots are discovered by scanning
for the state files they write (see adapters/discovery.py), so starting a new
bot is enough to make it appear — no config change, no restart. What *is*
configured here is where to scan and which exchanges have market-data
support.
"""

from __future__ import annotations

import os

from adapters.bitunix_account import BitunixAccountAdapter
from adapters.bitunix_klines import BitunixKlineAdapter
from adapters.coinbase_account import CoinbaseAccountAdapter
from adapters.coinbase_klines import CoinbaseKlineAdapter
from adapters.okx_account import OkxAccountAdapter
from adapters.okx_klines import OkxKlineAdapter

# Roots scanned for bot state files. Globs, colon-separated, overridable so
# the dashboard can watch a different install without a code change. The
# defaults cover the /root/bots/<bot>/<leg>/ layout this fleet uses.
VIZ_GLOBS = os.environ.get(
    "VIZ_GLOBS",
    "/root/bots/*/*/.*viz*.json:/root/bots/*/.*viz*.json",
).split(":")

FILLS_GLOBS = os.environ.get(
    "FILLS_GLOBS",
    "/root/.*_tracker/fills.jsonl:/root/bots/*/fills.jsonl",
).split(":")

# How often the filesystem is rescanned for new or vanished bots. This is
# the worst-case delay between a bot starting and appearing on screen.
DISCOVERY_INTERVAL_S = float(os.environ.get("DISCOVERY_INTERVAL_S", "15.0"))

# How many fills each bot keeps for its performance window. Larger windows
# make realised PnL less distorted by truncation (the lots that opened the
# current position are more likely to fall inside the window) at the cost of
# memory per bot.
FILLS_MAXLEN = int(os.environ.get("FILLS_MAXLEN", "2000"))

# How many samples the basis sparkline keeps per venue pair. At the default
# 1.5s frontend poll this is ~6 minutes of history — enough to see a basis
# actually moving, not just its instantaneous value.
BASIS_HISTORY_LEN = int(os.environ.get("BASIS_HISTORY_LEN", "240"))

# Freshness thresholds a bot's state is judged against, in seconds. Mirrored
# in static/app.js as BOT_STALE_MS/BOT_DEAD_MS (milliseconds there) — kept in
# sync by hand, not shared code, because one is Python judging a background
# thread and the other is JS judging a poll response; change both together.
BOT_STALE_S = float(os.environ.get("BOT_STALE_S", "15.0"))
BOT_DEAD_S = float(os.environ.get("BOT_DEAD_S", "60.0"))

# How often the background thread re-checks every bot's freshness for the
# incident timeline. This runs independently of any browser polling the
# dashboard, so a halt that happens while no tab is open is still recorded —
# the entire point of a timeline instead of a live badge.
STATE_POLL_S = float(os.environ.get("STATE_POLL_S", "5.0"))

# Per-bot transition history depth, and how many of the most recent
# transitions (across all bots) the incidents feed returns.
STATE_HISTORY_MAXLEN = int(os.environ.get("STATE_HISTORY_MAXLEN", "50"))
INCIDENTS_LIMIT = int(os.environ.get("INCIDENTS_LIMIT", "30"))

POLL_INTERVAL_S = float(os.environ.get("POLL_INTERVAL_S", "1.0"))
ACCOUNT_POLL_INTERVAL_S = float(os.environ.get("ACCOUNT_POLL_INTERVAL_S", "5.0"))
KLINE_POLL_INTERVAL_S = float(os.environ.get("KLINE_POLL_INTERVAL_S", "20.0"))

BIND_HOST = os.environ.get("BIND_HOST", "127.0.0.1")
BIND_PORT = int(os.environ.get("BIND_PORT", "8091"))


def build_kline_adapter(exchange: str, symbol: str):
    """Candles for a discovered bot, or None if that exchange has no
    market-data adapter yet. A bot on an unsupported exchange still shows
    its book, position, quotes and performance — it just has no chart,
    which is a far better outcome than not appearing at all."""
    if exchange == "bitunix":
        return BitunixKlineAdapter(symbol=symbol, poll_interval_s=KLINE_POLL_INTERVAL_S)
    if exchange == "coinbase":
        return CoinbaseKlineAdapter(symbol=symbol, poll_interval_s=KLINE_POLL_INTERVAL_S)
    if exchange == "okx":
        return OkxKlineAdapter(symbol=symbol, poll_interval_s=KLINE_POLL_INTERVAL_S)
    return None


def build_account_adapter(exchange: str, symbol: str):
    """Account margin, only where credentials exist for that exchange.

    `symbol` is unused by Bitunix/OKX (their account endpoint is scoped by
    margin coin, not by product) but Coinbase needs it to pick the right
    quote-currency balance out of a spot account list.
    """
    if exchange == "bitunix":
        api_key = os.environ.get("BITUNIX_API_KEY")
        secret_key = os.environ.get("BITUNIX_SECRET_KEY")
        if not api_key or not secret_key:
            return None
        return BitunixAccountAdapter(
            api_key=api_key,
            secret_key=secret_key,
            poll_interval_s=ACCOUNT_POLL_INTERVAL_S,
        )
    if exchange == "okx":
        api_key = os.environ.get("OKX_API_KEY")
        secret_key = os.environ.get("OKX_SECRET_KEY")
        passphrase = os.environ.get("OKX_PASSPHRASE")
        if not api_key or not secret_key or not passphrase:
            return None
        return OkxAccountAdapter(
            api_key=api_key,
            secret_key=secret_key,
            passphrase=passphrase,
            poll_interval_s=ACCOUNT_POLL_INTERVAL_S,
        )
    if exchange == "coinbase":
        # COINBASE_KEY_FILE matches the env var name the bot itself already
        # uses (see v17mm's .env) — reusing it means no new credential has
        # to be minted just for this dashboard. COINBASE_API_KEY/SECRET is
        # the fallback for a raw key/secret pair instead of a key file.
        key_file = os.environ.get("COINBASE_KEY_FILE")
        api_key = os.environ.get("COINBASE_API_KEY")
        api_secret = os.environ.get("COINBASE_API_SECRET")
        if not key_file and not (api_key and api_secret):
            return None
        return CoinbaseAccountAdapter(
            symbol=symbol,
            key_file=key_file,
            api_key=api_key,
            api_secret=api_secret,
            poll_interval_s=ACCOUNT_POLL_INTERVAL_S,
        )
    return None
