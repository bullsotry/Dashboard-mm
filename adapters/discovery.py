"""Find running bots by looking at what they write, not by being told.

The dashboard is meant to be left running permanently: a bot that starts
after the dashboard did must show up on its own, and one that stops must be
visibly dead rather than silently missing. Nothing here is configured
per-bot — a bot is discovered because its state file exists.

WHY THE FILE IS THE SOURCE OF IDENTITY
--------------------------------------
Each bot's viz file already carries `exchange` and `product_id`. Reading the
identity out of the file means a new bot needs no entry anywhere in this
repo: drop a bot that writes the same shape into one of the scanned roots
and it appears. The alternative — a registry mapping paths to names — goes
stale the moment a bot moves, and goes stale silently.

A discovered bot is not necessarily a *running* bot. Discovery only proves a
file exists; liveness is the timestamp inside it, which the frontend renders
separately. A bot stopped three days ago is still discovered, and is shown
as dead. That is deliberate: silently dropping it from the list would look
identical to it never having existed.
"""

from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DiscoveredBot:
    key: str  # stable id, e.g. "bitunix:SOLUSDT"
    exchange: str
    symbol: str
    viz_path: Path
    # Every ledger matched by FILLS_GLOBS, not just one. A fleet commonly
    # splits fills across several concurrently-live files (one tracker per
    # venue, e.g. fills.jsonl for bitunix+coinbase and fills_okx.jsonl for
    # OKX) — each bot's own venue/symbol filter in BotStateAdapter picks its
    # rows back out, so tailing all of them is safe and picking only the
    # most-recently-written one silently starves whichever ledger isn't it.
    fills_paths: tuple[Path, ...]

    @property
    def label(self) -> str:
        return f"{self.exchange} · {self.symbol}"


def _exchange_from_path(path: Path) -> str:
    """Last-resort venue name, taken from the directory holding the file.

    Used only when a bot doesn't publish `exchange`. Guessing from
    `/root/bots/v17mm/bitunix_mm/...` -> "bitunix" is imperfect, but the
    alternative is refusing to list the bot at all, which would make it
    invisible for exactly the reason the operator would never think to
    check. A wrong label is visible and fixable; a missing bot is not.
    """
    name = path.parent.name.lower()
    for suffix in ("_mm", "-mm", "_bot", "-bot"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name or "unknown"


def _read_identity(path: Path) -> tuple[str, str] | None:
    """(exchange, symbol) from a viz file, or None if it isn't one.

    Deliberately tolerant: these files are rewritten continuously by a
    running bot, so a read can land mid-write. A failure here means "skip
    this file on this scan", never an exception into the poll loop.
    """
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    exchange = str(data.get("exchange") or "").strip() or _exchange_from_path(path)
    symbol = str(data.get("product_id") or data.get("symbol") or "").strip()
    if not symbol:
        return None
    # A viz file without an orderbook is not something this dashboard can
    # render; skipping it here keeps unrelated json out of the bot list.
    if "orderbook" not in data:
        return None
    return exchange, symbol


def _pick_fills_paths(candidates: list[str]) -> tuple[Path, ...]:
    # All of them, not just the freshest — see the fills_paths docstring on
    # DiscoveredBot for why picking a single winner drops other bots' fills.
    existing = {Path(p) for p in candidates if os.path.isfile(p)}
    return tuple(sorted(existing))


def discover(
    viz_globs: list[str],
    fills_globs: list[str],
    exclude: list[str] | None = None,
) -> list[DiscoveredBot]:
    fills_paths = _pick_fills_paths(
        [p for pattern in fills_globs for p in glob.glob(pattern)]
    )

    exclude = exclude or []
    seen: dict[str, DiscoveredBot] = {}
    for pattern in viz_globs:
        for raw in glob.glob(pattern):
            # A shadow/paper copy publishes the same identity as the bot it
            # mirrors, so it collides on the same key and the mtime tiebreak
            # below silently hands the key over the moment the live bot
            # stops. Excluded by path, before identity is even read.
            if any(frag in raw for frag in exclude):
                continue
            path = Path(raw)
            identity = _read_identity(path)
            if identity is None:
                continue
            exchange, symbol = identity
            key = f"{exchange}:{symbol}"
            # Two files claiming the same identity: keep the fresher one.
            # This happens when a bot directory is copied for a test run.
            if key in seen:
                try:
                    if path.stat().st_mtime <= seen[key].viz_path.stat().st_mtime:
                        continue
                except OSError:
                    continue
            seen[key] = DiscoveredBot(
                key=key,
                exchange=exchange,
                symbol=symbol,
                viz_path=path,
                fills_paths=fills_paths,
            )

    # Stable ordering so the frontend's tab order doesn't shuffle between
    # scans; the frontend decides which one to select, not this.
    return sorted(seen.values(), key=lambda b: b.key)
