# Dashboard MM

Real-time, **read-only** monitoring dashboard for the v17mm market-making
fleet — Bitunix, Coinbase and OKX legs, whichever of them are running.

It renders a candlestick chart (real klines from whichever venue the
selected bot trades on, with a volume histogram) with buy/sell fill markers,
the bot's own resting quotes and entry prices drawn as price lines, a
position & margin panel, a performance panel, a toggleable order book, and a
Curve/Delta/Volume/NAV panel pair — cumulative realised PnL, running
inventory, cumulative traded notional and account equity, each as a mini
chart instead of just a single number. The backend is a single FastAPI route
(`/snapshot`) that the frontend polls; everything else is a static file.

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
- **Bots are discovered, not configured.** There is no list of bots anywhere.
  The server scans for the state files bots write and reads each bot's
  identity out of the file itself, so a bot started long after the dashboard
  appears on its own within seconds, and one that stops is shown as dead
  rather than silently vanishing. Adding a venue means adding market-data
  adapters for it; a bot on an unsupported exchange still shows its book,
  position, quotes and performance, just without candles.
- **Per-venue adapters, one contract.** Klines and account/margin are each
  behind the same shape (`get_klines`, `get_account`) regardless of venue —
  `config.build_kline_adapter`/`build_account_adapter` pick the right one by
  the bot's own declared `exchange`. A venue with no adapter yet just omits
  that panel instead of erroring.

## Layout

```
server.py                 FastAPI app, one /snapshot route + static mount
config.py                 paths, symbols, venue registry (all env-overridable)
adapters/
  base.py                 shared dataclasses
  discovery.py            finds bots by scanning for the files they write
  stats.py                FIFO realised PnL + fill metrics (pure, no I/O)
  bot_files.py            reads one bot's state (orderbook, positions, fills, quotes)
  basis.py                pure: pairs legs sharing a base asset, mid gap in bps
  bitunix_account.py      Bitunix REST, signed, GET-only (margin/equity)
  bitunix_klines.py       Bitunix REST, public (candles, 8 timeframes)
  coinbase_account.py     Coinbase Advanced Trade SDK (quote-currency balance; spot, no margin)
  coinbase_klines.py      Coinbase Advanced Trade REST, public (candles)
  okx_account.py          OKX v5 REST, signed, GET-only; forces IPv4 (OKX's IP allow-list rejects IPv6)
  okx_klines.py           OKX v5 REST, public (resolves the rolling instId, then candles)
static/                   index.html + app.js + fill_markers.js + charting vendor
tests/                    hand-computed cases for the PnL engine + basis pairing + adapters
deploy/                   systemd unit
```

## Cross-venue basis

For a bot that runs two legs on different exchanges (quote on one, hedge or
price off another), the basis panel shows the mid-price gap between them in
bps, with a rolling sparkline. It reads every discovered pair's orderbook
each poll — not just the currently selected bot — because a two-leg bot can
drift from its own reference while both legs individually look fine. Pairing
is by normalised base asset (`SOLUSDT` and `SOL-USD` both key to `SOL`); two
legs on the *same* exchange are never paired, since that would be a naming
collision, not a basis. See `adapters/basis.py`.

## Portfolio row & incident timeline

The bot list doubles as a unified portfolio table: each row shows the bot's
net position, side and combined uPnL without having to select it first — a
dead bot shows "stopped" instead of its last frozen numbers, same refusal as
the rest of the dashboard.

A background thread (`_background_state_loop` in `server.py`, `STATE_POLL_S`
apart) watches every discovered bot's freshness on its own clock and logs
live/warn/dead transitions to an in-memory ring buffer, independent of
whether a browser tab is open polling `/snapshot`. The incidents panel is
that log: a halt that happened overnight while nobody was watching still
shows up when you open the dashboard the next morning. History is
process-memory only — it resets on a server restart, and the first
observation of a bot is discovery, not an incident, so it is never logged.

## Reading the performance panel

Every figure is computed over a **bounded window** — the last N fills the
adapter holds — and the panel header states the window it actually covers.
`realised gross` is FIFO round-trip PnL with fees excluded; `fees` are what
the venue reported per fill; `realised net` is the only number that reflects
a result. Open inventory is deliberately *not* marked to market, and is shown
separately, so a settled figure is never mixed with a floating one. The limits
of this calculation are documented at the top of `adapters/stats.py`.

## Curve, Delta, Volume & NAV

Four running series, built from the same data two different ways:

- **Curve** (cumulative realised PnL) and **Delta** (running inventory) are
  the Performance panel's FIFO replay (`adapters/stats.py:equity_curve`),
  plotted point-by-point instead of collapsed to a total. They refuse to
  render under the same condition the Performance panel does — see
  `pnl_unreliable` below — because a replay that can't defend its total
  can't defend any point on its curve either. The Curve header also shows
  **max drawdown**: the largest peak-to-trough drop the realised-PnL curve
  has taken over the window, updated every point. It's derived from
  `realised_net`, so it inherits the same refusal — a curve that can't
  defend its total can't defend its drawdown either.
- **Volume** (cumulative traded notional) comes from the same replay but
  does *not* depend on FIFO lot matching — it's a plain running sum of
  `price * size` — so it stays honest and keeps rendering even on a window
  Curve/Delta have refused, e.g. right after a restart before the ledger has
  caught up with the exchange.
- **NAV** (account equity) is different in kind: it's not derived from fills
  at all, it's `available + margin_used + unrealised_pnl` off each venue's
  own account adapter (OKX reports equity directly; Bitunix and Coinbase
  derive it the same way). Because it isn't a replay of stored fills, there
  is no ledger to rebuild history from — the curve is whatever this
  dashboard has sampled, one point per poll, while it's been open. A first
  load on a freshly started dashboard starts with one point, not history.

## Tests

```bash
venv/bin/python -m pytest tests/ -p no:anchorpy
```

(`-p no:anchorpy` sidesteps an unrelated broken pytest plugin some machines
have installed globally; harmless to include even where it isn't needed.)

## Running locally

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env      # optional: only needed for the margin panels
./run.sh
```

Then open <http://127.0.0.1:8091>.

Without credentials the dashboard still runs — every venue's margin panel
simply reports that account data is unavailable, and the chart, fills,
positions and order book all work regardless.

## Deployment

See [DEPLOY.md](DEPLOY.md). Security policy and threat model:
[SECURITY.md](SECURITY.md).
