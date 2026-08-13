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
venv/bin/python -m pytest tests/ -p no:anchorpy
```

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
npm install jsdom                                  # once
node tests/frontend/dom_smoke.js
SEED_BOOK=420 node tests/frontend/dom_smoke.js     # with saved layout state
```

`tests/test_app_js_declarations.py` guards the specific shape that caused
it (a function declared twice at top level, hoisting over a `const` still
in its temporal dead zone) and does run in the pytest suite.

## Docs stay in sync

`README.md` documents behavior; this file documents conventions for making
changes. When a change adds a panel, a metric, or an adapter, update
`README.md` in the same commit — a stale README is worse than no README,
same principle as the honesty rule above.
