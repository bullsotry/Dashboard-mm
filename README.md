# Dashboard MM

Real-time, **read-only** monitoring dashboard for the v17mm market-making
fleet — Bitunix, Coinbase and OKX legs, whichever of them are running.

It renders a candlestick chart (real klines from whichever venue the
selected bot trades on, with a volume histogram) with buy/sell fill markers,
the bot's own resting quotes and entry prices drawn as price lines, and,
directly under the chart, a Curve/Delta/Volume panel — cumulative realised
PnL, running inventory and cumulative traded notional, each as a mini chart
laid out side by side instead of just a single number. A side rail carries
the bot list, cross-venue basis, incidents, order book, position & margin,
performance panel and NAV. The backend is a small FastAPI app: `/snapshot`
returns the current state, `/stream` pushes it as it changes, and everything
else is a static file.

Every panel has a grip along its bottom edge: drag it to resize that panel,
double-click to reset. The drag moves the **box** 1:1 with the mouse and
nothing else: the type is never scaled, so a taller panel shows more rows
rather than bigger rows, and every glyph stays at the integer pixel size it
was authored at. Content that stops fitting scrolls inside the panel. The
title bar also carries `hide`, collapsing the panel to just that bar. Both choices persist per panel in `localStorage`,
as do the two column widths (drag the dividers either side of the order
book). The charts and the order book draw into a canvas and are left at
their true resolution — they re-render at the new size rather than being
scaled, which would blur them and desynchronise `price_tags.js`'s pixel
hit-testing.

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

A poll that hangs is treated as a poll that failed. Each request carries a
timeout of 3x the poll interval, because a fetch against a tunnel that has
gone silent settles neither way for minutes — and the "one request in flight
at a time" guard would then hold forever, leaving a page that ages its badge
honestly while having quietly stopped asking. It recovers on its own when
the link comes back.

## Nothing the operator waits on talks to a venue

Every venue round trip happens on a background thread, never inside
`/snapshot`. Account margin and candles are both refreshed by warm loops
(`server._account_warm_loop`, `server._kline_warm_loop`) that tick at half
the adapters' own poll interval, so a request finds a warm cache and returns
without touching the network. Requests ask for candles with
`serve_only=True`, which answers from cache and declines to fetch even when
the chart could be extended further back — that is worth a background round
trip, never a request someone is watching. The one exception is a cold
cache, which fetches the most recent page and only that: one round trip
instead of up to eight, so a chart appears fast and fills in its history
behind itself.

The warm loop refreshes only what the UI has actually asked for in the last
`KLINE_WARM_TTL_S` (120s). Warming all eight granularities of every bot
would multiply this dashboard's REST footprint for candles nobody is
looking at.

Responses are gzipped (`compresslevel=5`), which matters because the whole
thing is read over an SSH tunnel: a snapshot is a couple of hundred KB of
JSON resent every poll.

## How the screen gets its data

Two transports, one of which is a fallback for the other.

`/stream` is server-sent events: the server rebuilds a snapshot every
`STREAM_INTERVAL_S` (0.5s) and pushes it only when it differs, plus a
heartbeat every `STREAM_HEARTBEAT_S` (3s) when it does not. `/snapshot` is
the plain poll, every `POLL_MS` (750ms), and the frontend stands it down
while the stream is delivering — a stream that stops for any reason (no
`EventSource`, a buffering proxy, a dropped tunnel) hands the next tick back
to the poll within 8s, with no state machine in between.

Why bother: measured over the real tunnel, the server does 8-20ms of work
and the round trip costs ~240ms, so the poll *interval* dominated how stale
the screen was — average staleness is half the interval plus the trip.
Pushing removes the interval and the request leg both.

Two properties this must never lose, each guarded by a test:

- **A heartbeat proves the link, never the bot.** Under a push transport,
  silence is the normal state of a healthy link, so something has to
  distinguish it from a dead one. But bot age must keep counting from the
  bot's own last write — a heartbeat that reset it would put a bot which
  stopped writing hours ago behind a green "live" badge.
- **`/stream` is exempt from gzip.** `GzipFile` buffers until it has enough
  input or is closed, and this response never closes, so a compressed
  stream can connect successfully and then deliver nothing at all.

## Several tabs are several threads

`/snapshot` is a sync route, so FastAPI serves it from a threadpool: two
open tabs, or a forced poll landing on a pending one, put several threads
inside the same cached adapter at once, and the warm loops add more. Every
adapter is therefore internally locked, in one of two ways depending on
whether its critical section touches the network — see `adapters/_locking.py`
and the concurrency rules in CLAUDE.md. This is not theoretical tidiness:
before the locks, eight threads reading one 3000-row fill ledger tailed it
as 20000 rows and reported a realised PnL of ~9798 against a true 1470.

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
  stats.py                FIFO realised PnL + cash-flow PnL + fill metrics (pure, no I/O)
  sessions.py             pure: session bounds, read from the tracker's own session log
  markouts.py             pure: post-fill mid drift per horizon, signed by side
  _locking.py             shared thread-safety plumbing (see 'Several tabs' above)
  bot_files.py            reads one bot's state (orderbook, positions, fills, quotes)
  basis.py                pure: pairs legs sharing a base asset, mid gap in bps
  bitunix_account.py      Bitunix REST, signed, GET-only (margin/equity)
  bitunix_klines.py       Bitunix REST, public (candles, 8 timeframes)
  coinbase_account.py     Coinbase Advanced Trade SDK (quote-currency balance; spot, no margin)
  coinbase_klines.py      Coinbase Advanced Trade REST, public (candles)
  okx_account.py          OKX v5 REST, signed, GET-only; forces IPv4 (OKX's IP allow-list rejects IPv6)
  okx_klines.py           OKX v5 REST, public (resolves the rolling instId, then candles)
static/                   index.html + app.js + fill_markers.js + price_tags.js + charting vendor
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

A leg's mid is only used while it is fresh. The mid comes from the bot's own
published book, so it *freezes* rather than disappears when that bot stops —
and a frozen mid paired against a live one yields a large, stable, entirely
fictional basis whose sparkline still moves (because the live leg moves).
Any leg older than `BASIS_MAX_AGE_S` (30s) is therefore dropped before
pairing and listed by name with its age, so the panel says what it refused
to price instead of quietly showing fewer rows. Each surviving pair also
carries the **skew** between its two mids: the legs are not sampled at the
same instant (on this fleet, ~0.3s old on Bitunix vs ~12s on Coinbase), and
on a fast asset that skew is worth bps of its own — the same order of
magnitude as the basis itself.

## Performance is measured per session

A session is one run of the bot. The Performance panel covers the running
session by default, with a dropdown to read back any earlier one — and a
stopped bot keeps its sessions, since what it did last night is settled
history, not a stale reading (only its live panels — book, position, NAV —
are refused once it dies).

Bounds are **not** detected here. This fleet already runs a session recorder
next to each bot (`v17mm-tracker.service`, started and stopped by the same
unit), which appends `start,end,clean_exit,…` to `sessions*_all.csv` beside
the fill ledger. Those bounds come from the process lifecycle itself, so
`adapters/sessions.py` reads them rather than guessing. Only the *bounds*
are taken from that file: every figure is recomputed here from the fills,
because the tracker's own PnL uses different conventions and two different
numbers for the same session would be worse than the problem this dashboard
exists to solve. The one session the CSV cannot contain is the running one
(its row is written on shutdown), so that start comes from watching the bot
come back to life, or failing that from its first fill since the previous
session ended — the panel says which.

Fills *before* the session start are still replayed, without being counted.
A bot almost never restarts flat, so a window that simply dropped them would
open at inventory 0 while the exchange holds a real position, and the
session's first sells would match against lots that were never bought. What
that rebuild produces is then checked against the inventory the bot itself
recorded on the session's first fill; if they disagree, the ledger doesn't
reach back far enough and the realised figure is refused rather than shown.

### Two PnL figures, on purpose

| | `realised net` | `session pnl (cash)` |
|---|---|---|
| Method | FIFO round-trip matching | cash in − cash out ± inventory marked |
| Hedge mode | refused — FIFO cannot tell which book a fill moved | unaffected, it matches nothing |
| Open inventory | excluded | included, marked at the last mid |

The Bitunix leg runs a long and a short simultaneously as a matter of
course, so its realised figure is permanently and correctly refused — which
left the venue doing most of the volume with no answer at all. The cash-flow
figure answers without matching lots. It is not the same number and is never
relabelled as one: it marks open inventory to market, so the two converge
only when a session ends flat, and the gap between them *is* the open
exposure. Cross-checked against the account's own equity delta on five live
sessions: the three whose opening inventory came from the ledger agreed to
within 0.15 USD.

It is withheld where the ledger's inventory is not denominated in the same
units as its fill sizes — OKX rows count contracts while `size` counts base,
which valued a 10-SOL book as a 1026-SOL one.

`capture` is withheld when the flow is more than 55/45 one-sided: past that
it measures where price went between the buying and the selling, not what
the quotes earned.

## Markout

The measure that separates market making from being picked off. A maker
earns the spread at the moment of the fill and keeps it only if the mid
doesn't walk away afterwards; `capture` is measured at t=0 and is silent on
that. The tracker already sampled the mid at 100ms/1s/5s/10s/30s after every
fill (`markouts.jsonl`) — nothing read it until this panel.

Signed so positive always means the market moved your way, reported per
horizon and split by side (a book run over on one side only is a real and
common failure that a blended average hides).

Only *relative* movement is computed — both terms share the mid at fill —
and the absolute edge (fill price vs mid) deliberately is not. `fill_mid` is
captured when the fill is processed, i.e. after execution, so a passive
maker's own fill removes its level and the mid recoils against it: measured
here, that made 50% of Bitunix and 59% of Coinbase fills *look* like they
executed on the wrong side of the mid. Any absolute edge built on that field
inherits the artefact; differences cancel it.

Markout rows carry `venue` but no `symbol`, so they are matched to this
bot's fills by `trade_id` when the venue carries more than one symbol, and
by venue + time window when it carries only one (Coinbase's tracker
aggregates fills per order while markouts are per execution, so the ids
genuinely don't correspond there). The panel says which was used.

Note that `side` casing differs *between venues in the same file* —
`BUY`/`SELL` on Bitunix, `buy`/`sell` on Coinbase. Normalising it is not
cosmetic: comparing against `"buy"` alone reads every Bitunix buy as a sell
and inverts the sign of the result.

## Fees

Fee sign conventions differ by venue: Bitunix and Coinbase record a charged
fee as positive, OKX records a charge as *negative* (positive means rebate).
`adapters/bot_files.py` normalises every fee to a positive cost at ingestion,
so `realised_net = realised_gross - fees` subtracts what was actually paid.
Read raw, OKX's convention made the dashboard add fees to PnL: a real
-0.80 USD showed as +5.20 over a 931-fill window. A venue that genuinely
pays a rebate needs explicit signed handling there — not an inferred sign.

## Contracts vs base units

Position sizes are not in the same unit on every venue, and the field name
does not say so: the OKX engine publishes `net_position_base: 2489` while
holding **24.89 SOL** — its instrument is denominated in contracts of 0.01
SOL. Compared raw against a fills replay, which *is* in base units, the
reliability check could only ever fail; it refused the Curve, Delta and
Performance panels for a reason that had nothing to do with the ledger.

`adapters/bot_files.py` scales the viz position by `contract_value`, read
from the fill ledger itself — the tracker writes it on every row, having
got it from the venue's own instrument definition. Deliberately *not*
configured in this repo: a second copy of that number here would be a guess
that goes stale the day a bot changes instrument. A ledger that declares
nothing is left unscaled (`1.0`), which keeps the failure pointing the safe
way — the replay disagrees with the position and the panel refuses, rather
than showing a converted number nobody can defend. And when the two differ
by an exact round factor of 10 or more, the refusal says so: that shape is a
unit mismatch, not a missing history.

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

Every figure covers one session, and the panel header names it. `realised
gross` is FIFO round-trip PnL with fees excluded; `fees` are what the venue
reported per fill, normalised to a cost; `realised net` is the settled
result. Open inventory is deliberately *not* marked to market there, and is
shown separately, so a settled figure is never mixed with a floating one —
`session pnl (cash)` is the figure that does include it, labelled. The limits
of both calculations are documented at the top of `adapters/stats.py`.

Three distinct states, deliberately not collapsed into one:

- **refused** (`pnl_unreliable`) — positive evidence the figure would be
  wrong: a gap in the engine's own fill counter, hedge mode, a ledger that
  disagrees with the exchange, an opening position the pre-roll couldn't
  rebuild. The number is withheld and the reason shown.

  The counter is the only one of those that *proves* rather than infers.
  The trackers sample the bot's recent-fills deque on a timer, so a fill
  they never sampled leaves no trace at all — the loss used to surface much
  later, and only as an inventory the ledger disagreed with. Each engine now
  stamps every fill it records with `fill_seq` (plus a `fill_run_id`
  scoping it to one process), so consecutive rows of one run must differ by
  exactly 1 and a hole is countable: *"3 fill(s) the recorder never wrote
  down"*. It is checked over the reported window only — a hole from last
  week does not make tonight unmeasurable — it says nothing about rows
  written before the engines emitted it, and it cannot see fills that
  happened while the bot itself was down. It proves a ledger has holes,
  never that it has none.
- **unverified** (`pnl_unverified`) — no way to check it either way, because
  the bot publishes no position to compare the replay against. The number is
  shown, with the caveat. This state exists because conflating it with
  "fine" made the Coinbase leg the one PnL on screen presented as
  trustworthy, precisely because it was the only one that could not be
  tested.
- **clean** — the replay was checked against the exchange and agreed.

## Curve, Delta, Volume & NAV

Four running series, built from the same data two different ways. Curve,
Delta and Volume sit in their own panel directly under the main chart, side
by side rather than stacked — they're a replay of the same fills the chart
already shows, so they read better next to it than buried in the side rail.
NAV stays in the side rail, next to Position & Margin, since it comes from
the account adapter rather than the fill replay.

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
  at all, it's read off each venue's own account adapter, and the formula is
  not the same one twice — OKX reports it directly (`eq`), Bitunix derives
  `available + frozen + margin_used + upnl + bonus`, Coinbase derives
  `available + held` (spot only, no margin/unrealised terms to add).
  `frozen` — capital locked by *resting orders* — is in Bitunix's sum
  because leaving it out is not a rounding error: it once held 92.43 USDT
  against an `available` of 1.73, so the panel reported a 14.42 NAV on a
  106.95 account and made it lurch every time the bot placed or pulled a
  quote. An equity that omits order-locked capital measures quoting
  activity, not net assets.

  **The scope is not the same on every venue**, so it is printed next to the
  figure rather than assumed: Bitunix is the futures account, Coinbase is a
  quote-currency balance that excludes the base asset held, OKX is one
  settlement currency out of the twelve that account holds (its venue-wide
  `totalEq` is shown separately as "account total", never folded in — it
  spans products this bot has nothing to do with). Summing the three NAVs
  across venues does not produce a portfolio value.

  Because it isn't a replay of stored fills, there is no ledger to rebuild
  history from — the curve is whatever this dashboard has sampled, one point
  per poll, while a browser has been open, and the header states that span.
  It is minutes long, it shrinks when several tabs share the buffer, and it
  does not accumulate at all while nobody is watching.

## Tests

```bash
venv/bin/pip install -r requirements-dev.txt   # once: pytest + httpx
venv/bin/python -m pytest tests/ -p no:anchorpy

npm install                                     # once: jsdom
node tests/frontend/dom_smoke.js                # poll-only path
SEED_BOOK=420 node tests/frontend/dom_smoke.js  # with saved layout state
STREAM=1 node tests/frontend/dom_smoke.js       # push path
```

(`-p no:anchorpy` sidesteps an unrelated broken pytest plugin some machines
have installed globally; harmless to include even where it isn't needed.)

The three frontend runs cannot be collapsed into one: a healthy stream
deliberately stands the poll down, so the poll-timeout check and the stream
checks need separate loads of the page.

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
