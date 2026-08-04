# Security

## Threat model

This dashboard is an **observer**. It reads a trading bot's state files and
two Bitunix REST endpoints, and serves the result over HTTP. It has no write
path of any kind: no order placement, no bot imports, no writes to files the
bot reads.

The assets worth protecting are, in order:

1. **The Bitunix API credentials** used for the margin panel.
2. **The bot's live state** (positions, fills, quotes) — commercially
   sensitive, not something to serve publicly.
3. **The host itself** — the service runs as root to read `600 root:root`
   files, so any remote code execution here would be severe.

## Controls in place

| Control | Where |
|---|---|
| No secrets in the repo; credentials only via environment | `config.py`, `.env` is gitignored |
| Missing credentials degrade gracefully instead of failing open | `config.build_bitunix_account_adapter()` returns `None` |
| Listens on `127.0.0.1` by default, never `0.0.0.0` | `config.BIND_HOST`, systemd `ExecStart` |
| Access via SSH tunnel, no port exposed to the internet | `DEPLOY.md` step 6 |
| Filesystem is read-only to the process, kernel-enforced | `deploy/dashboard-mm.service` (`ProtectSystem=strict`, `ProtectHome=read-only`) |
| No privilege escalation | `NoNewPrivileges=yes`, `RestrictSUIDSGID=yes` |
| Only GET requests are ever sent to the venue | `adapters/bitunix_account.py` |
| Query parameters validated at the boundary before reaching an external call | `server.py::snapshot` |
| Infrastructure hostnames kept out of the repo | `DEPLOY.md` uses `$VPS` |

## API key requirements

The key this dashboard uses **must** be a dedicated key with **trade and
withdraw permissions unchecked**. Never reuse the trading bot's key. The
dashboard only ever calls `GET /api/v1/futures/account`; a key with trading
rights grants far more authority than the application needs.

Create the `.env` directly on the target host (`umask 077`, then `chmod 600`)
so the secret never enters local shell history.

## Known limitations

- **There is no authentication or authorization in the application.** Anyone
  who can reach the port sees everything. This is deliberate: the security
  boundary is the network (localhost binding + SSH tunnel), not the app. If
  you ever expose this beyond localhost, you must add auth and TLS first —
  binding to `0.0.0.0` without that publishes your live trading state.
- **No rate limiting.** Not needed while access is localhost-only; needed the
  moment it is not.
- **The service runs as root** to read the bot's `600 root:root` state files.
  The systemd hardening above is what keeps that acceptable; do not remove it.

## Reporting a vulnerability

Open a GitHub issue for anything non-sensitive. For a finding that would
expose credentials or the host, use GitHub's private vulnerability reporting
rather than a public issue.
