"""Read-only monitoring dashboard. Never places orders, never imports the
bot's code, never writes to anything the bot reads. Single route (/snapshot)
that the frontend polls; everything else is a static file.
"""

from __future__ import annotations

import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import config
from adapters.bitunix_klines import DEFAULT_INTERVAL, SUPPORTED_INTERVALS, interval_seconds

app = FastAPI()

_STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

# v1 is single-venue; the loop below still walks config.VENUES so adding a
# second venue later is a config change, not a server.py change.
_file_adapters = {name: v["file_adapter"]() for name, v in config.VENUES.items()}
_account_adapters = {name: v["account_adapter"]() for name, v in config.VENUES.items()}
_kline_adapters = {name: v["kline_adapter"]() for name, v in config.VENUES.items()}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


@app.get("/snapshot")
def snapshot(interval: str = DEFAULT_INTERVAL) -> dict:
    # Validated here, at the boundary, before it reaches an external API call.
    if interval not in SUPPORTED_INTERVALS:
        interval = DEFAULT_INTERVAL

    venues = {}
    for name, file_adapter in _file_adapters.items():
        account_adapter = _account_adapters.get(name)
        kline_adapter = _kline_adapters.get(name)
        venues[name] = {
            "symbol": file_adapter.symbol,
            "orderbook": file_adapter.get_orderbook(),
            "positions": file_adapter.get_positions(),
            "fills": file_adapter.get_recent_fills(),
            "quotes": file_adapter.get_quotes(),
            "stats": file_adapter.get_stats(),
            # The bot's own heartbeat. Distinct from server_ts below: this
            # server answers happily while the bot it watches is dead, and
            # the frontend must be able to tell those two apart.
            "source_ts": file_adapter.get_source_ts(),
            "account": account_adapter.get_account() if account_adapter else None,
            "klines": kline_adapter.get_klines(interval) if kline_adapter else [],
            "kline_interval": interval,
            "kline_interval_s": interval_seconds(interval),
            "supported_intervals": SUPPORTED_INTERVALS if kline_adapter else [],
        }
    # Server clock, so the frontend measures bot staleness against the same
    # clock that produced source_ts instead of against the browser's, which
    # can be minutes off and would make a healthy bot look dead.
    return {"server_ts": time.time(), "venues": venues}
