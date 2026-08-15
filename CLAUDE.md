# CLAUDE.md — Dashboard MM

Repo-specific conventions for Claude Code. See [README.md](README.md) for
architecture and layout; this file is about *how to change this codebase*
without breaking what makes it trustworthy.

## What this repo is, in one line

A read-only monitoring dashboard for the v17mm market-making fleet
(Bitunix/Coinbase/OKX). It never places orders, never imports bot code,
never writes anything a bot reads. Keep it that way — see
[SECURITY.md](SECURITY.md) for the full threat model before touching
anything that talks to a venue or reads bot state.

## The one rule that matters more than any other: never a confident lie

Every derived figure here (realised PnL, NAV, capture bps) is a measurement
over a bounded, possibly-incomplete window. The dashboard was built after a
figure was wrong in production and *looked* confident while being so (see
`adapters/stats.py`'s module docstring and `_reliability`). When adding a
new derived metric:

- If it can be wrong under some condition the code can detect (an
  incomplete ledger, hedge mode, a missing field), it must refuse to render
  rather than render a guess — set the equivalent of `pnl_unreliable` and
  make the frontend show *why*, not just a blank.
- If it genuinely cannot be wrong that way (e.g. `cum_volume_quote`, a
  plain sum that doesn't depend on FIFO lot matching), say so in a comment
  and don't gate it behind an unrelated reliability flag — gating it anyway
  would just hide a number that was fine.
- A number with no protocol behind it — no stated window, no stated venue,
  no stated "since when" — is not trustworthy. State the window.

## Adapter contract

One `VenueAdapter` Protocol (`adapters/base.py`), one file per venue per
concern (`bitunix_klines.py`, `bitunix_account.py`, ...). `config.py` picks
the right adapter by the bot's own declared `exchange` string —
`server.py` never imports a concrete adapter by name. Adding a venue means
writing a new adapter module + registering it in `config.py`; nothing else
should need to change. A venue with no account adapter yet just omits the
margin/NAV panel instead of erroring — every adapter method returns `None`
on failure/missing-creds rather than raising, and callers already handle
that.

`Account.equity` (NAV) has a different formula on every venue, not just a
different provenance — OKX reports it directly (`eq`), Bitunix derives
`available + frozen + margin_used + upnl + bonus` (futures account),
Coinbase derives `available + held` (spot only, no margin/unrealised
terms exist there). If you touch one of these, check whether the others
need the same fix; a NAV panel that means something different per venue is
worse than no NAV panel.

## Before calling anything done

```bash
venv/bin/pip install -r requirements-dev.txt   # once: pytest + httpx
venv/bin/python -m pytest tests/ -p no:anchorpy
```

Test-only dependencies live in `requirements-dev.txt`, never in
`requirements.txt` — the dashboard must not gain a runtime dependency
because a test needed one.

(`-p no:anchorpy` sidesteps a broken pytest plugin some machines have
installed globally — harmless to always include.) Every adapter has hand-
computed test cases with the arithmetic worked out in a comment, not just
"assert whatever the code returns" — match that style for new ones. For
anything that touches money-shaped numbers (PnL, NAV, volume), that's the
minimum bar, not the whole bar: if you can, also run the server locally
against a real or fixture bot and look at the actual panel.

**Touching `static/app.js` needs more than `node -c`.** A page that throws
partway through that file still renders and still polls — it just silently
stops wiring up every control below the throw, which reads as "the button
does nothing" rather than as an error. That shipped twice. `node -c` cannot
see it: the code is syntactically valid. Run the DOM smoke test, including
the seeded variant, because the last such bug only fired for users who had
already dragged the order-book divider:

```bash
npm install                                        # once, brings in jsdom
node tests/frontend/dom_smoke.js
SEED_BOOK=420 node tests/frontend/dom_smoke.js     # with saved layout state
STREAM=1 node tests/frontend/dom_smoke.js          # the push transport
```

Three runs, not one, and they cannot be merged: a healthy stream stands the
poll down on purpose, so the poll-timeout check and the stream checks need
separate loads of the page. jsdom has no `EventSource`, which is what makes
the default run cover the fallback for free.

Two more rules the drag controls have already broken once each:

- **A drag ends on four things, not one.** `setPointerCapture` is not a
  guarantee that a `pointerup` reaches the handle — the capture is dropped
  silently when the release happens outside the page or the browser loses
  the pointer, and the "still dragging" flag then survives the gesture, so
  the next mere *hover* over the handle resizes the layout with no button
  held. Every drag here ends on `pointerup`/`pointercancel`, on
  `e.buttons === 0` inside the move handler, on `lostpointercapture`, and
  on a window-level up. The end function is idempotent so all four can fire.
- **Never scale type to fit a box.** Not `transform: scale()`, not `zoom`,
  and not a `--pz` multiplier on `font-size` either — the last one shipped
  and read as blurry, because `12px * 1.4375` rasterises glyphs at 17.25px
  on fractional baselines. Resizing a panel changes the box; the content
  reflows and scrolls at its authored size. Same rule keeps the canvases
  sharp (they re-render via their `ResizeObserver`).

One rule for `/stream`, paid for during its own build:

- **Test the route, not just the function.** The stream's unit tests drive
  the route's async generator directly, because Starlette's `TestClient`
  runs the whole app to completion into a `BytesIO` before returning a
  response — `client.stream()` cannot read an endless response and simply
  hangs. That is the right call for the logic, but it means routing is
  never exercised: a `/stream` that is not registered at all would pass
  every one of those tests. Finish with a real `uvicorn` and a real
  `curl -N`, and read the frames.

Two concurrency rules, both paid for once:

- **Every adapter is shared by several threads.** `/snapshot` is a sync
  route (threadpool: one thread per in-flight request) and three warm loops
  run alongside it, all against the same cached adapter instances. Anything
  that reads-then-writes instance state — a tail offset, a memoised replay —
  must be locked. Before it was, eight threads reading one 3000-row ledger
  tailed it as 20000 rows and reported ~9798 of realised PnL against a true
  1470, with two threads disagreeing on the wrong answer.
- **Never hold a lock across a network call.** A kline backfill is up to
  eight pages at a 5s timeout; a plain mutex around it makes a reader wait
  the better part of a minute for data already in the cache — reintroducing
  the exact stall the warm loops exist to remove. Use
  `adapters/_locking.CacheGuard`: a data lock for dict merges only, plus a
  *non-blocking* fetch gate, so there is one in-flight fetch and everyone
  else serves the cache. `tests/test_concurrency.py` covers both.

`tests/test_app_js_declarations.py` guards the specific shape that caused
it (a function declared twice at top level, hoisting over a `const` still
in its temporal dead zone) and does run in the pytest suite.

## Docs stay in sync

`README.md` documents behavior; this file documents conventions for making
changes. When a change adds a panel, a metric, or an adapter, update
`README.md` in the same commit — a stale README is worse than no README,
same principle as the honesty rule above.
