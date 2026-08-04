# Dashboard MM

Real-time, **read-only** monitoring dashboard for a Bitunix market-making bot.

It renders a candlestick chart (real Bitunix klines) with buy/sell fill
markers, a position & margin panel, and a toggleable order book. The backend
is a single FastAPI route (`/snapshot`) that the frontend polls; everything
else is a static file.

## Design constraints

- **Read-only by construction.** It never places orders, never imports the
  bot's code, and never writes to anything the bot reads. The systemd unit
  enforces this at the kernel level (`ProtectSystem=strict`,
  `ProtectHome=read-only`) rather than by convention.
- **Binds to localhost only.** `BIND_HOST` defaults to `127.0.0.1`. There is
  no authentication layer, so the dashboard is reached over an SSH tunnel,
  never by exposing the port.
- **Venue-agnostic seams.** `config.VENUES` is the single registry; adding a
  venue is a config change plus an adapter module, not a `server.py` change.
  v1 ships one venue (Bitunix).

## Layout

```
server.py                 FastAPI app, one /snapshot route + static mount
config.py                 paths, symbols, venue registry (all env-overridable)
adapters/
  base.py                 shared dataclasses
  bitunix_files.py        reads the bot's state files (orderbook, positions, fills)
  bitunix_account.py      Bitunix REST, signed, GET-only (margin/equity)
  bitunix_klines.py       Bitunix REST, public (candles, 8 timeframes)
static/                   index.html + app.js + fill_markers.js + charting vendor
deploy/                   systemd unit
```

## Running locally

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env      # optional: only needed for the margin panel
./run.sh
```

Then open <http://127.0.0.1:8091>.

Without credentials the dashboard still runs — the margin panel simply reports
that account data is unavailable, and the chart, fills, positions and order
book all work.

## Deployment

See [DEPLOY.md](DEPLOY.md). Security policy and threat model:
[SECURITY.md](SECURITY.md).
