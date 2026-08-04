# Dashboard MM

Real-time, **read-only** monitoring dashboard for a Bitunix market-making bot.

It renders a candlestick chart (real Bitunix klines) with buy/sell fill
markers, the bot's own resting quotes and entry prices drawn as price lines,
a position & margin panel, a performance panel, and a toggleable order book.
The backend is a single FastAPI route (`/snapshot`) that the frontend polls;
everything else is a static file.

## Freshness is a first-class signal

The failure mode this dashboard is built to avoid is showing frozen data that
still looks alive. Two clocks are tracked and displayed separately, because
they break separately:

- **link** — can the browser still reach the server? (tunnel dropped, server died)
- **bot** — is the bot still writing its state file, even though the server answers?

The badge counts up on its own timer rather than on the poll loop, so it keeps
ageing during an outage instead of freezing at its last value. Anything other
than a clean live state desaturates the data panels, so stale numbers look
wrong from across the room rather than requiring you to read a small badge.

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
  stats.py                FIFO realised PnL + fill metrics (pure, no I/O)
  bitunix_files.py        reads the bot's state files (orderbook, positions, fills, quotes)
  bitunix_account.py      Bitunix REST, signed, GET-only (margin/equity)
  bitunix_klines.py       Bitunix REST, public (candles, 8 timeframes)
static/                   index.html + app.js + fill_markers.js + charting vendor
tests/                    hand-computed cases for the PnL engine
deploy/                   systemd unit
```

## Reading the performance panel

Every figure is computed over a **bounded window** — the last N fills the
adapter holds — and the panel header states the window it actually covers.
`realised gross` is FIFO round-trip PnL with fees excluded; `fees` are what
the venue reported per fill; `realised net` is the only number that reflects
a result. Open inventory is deliberately *not* marked to market, and is shown
separately, so a settled figure is never mixed with a floating one. The limits
of this calculation are documented at the top of `adapters/stats.py`.

## Tests

```bash
venv/bin/python tests/test_stats.py
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
