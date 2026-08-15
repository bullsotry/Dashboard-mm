# Deployment — single VPS, systemd

Read-only monitoring dashboard for the v17mm bot. Nothing here touches the
bot's code, files, or process.

Verified on the VPS before writing this: Python 3.12.3 + venv module present,
port 8091 free, `/root/dashboard-mm` does not exist, no `dashboard-mm.service`
exists, 135 G free.

## What the service reads

| Source | Access | Why root is required |
|---|---|---|
| `/root/bots/*/*/.*viz*.json` (whatever `VIZ_GLOBS` matches) | file read, ~1 s | mode `600 root:root` |
| `/root/.v17mm_tracker/fills.jsonl` | file read (tail), ~1 s | `/root` itself is mode `700` |
| Bitunix REST `/api/v1/futures/account` | signed HTTPS, 5 s | dashboard's own read-only key |
| Bitunix REST `/api/v1/futures/market/kline` | public HTTPS, 20 s | no key |
| OKX REST `/api/v5/account/balance` | signed HTTPS, 5 s, IPv4-forced | dashboard's own read-only key |
| OKX REST `/api/v5/public/instruments`, `/api/v5/market/candles` | public HTTPS, 20 s | no key |
| Coinbase Advanced Trade `get_accounts` (via SDK) | signed HTTPS, 5 s | dashboard's own key or key file |
| Coinbase REST `/api/v3/brokerage/market/.../candles` | public HTTPS, 20 s | no key |

Every credential-gated row is optional — see `.env.example`. A venue with no
key configured just shows no margin panel for its bots; discovery, book,
positions, fills and candles are unaffected.

`User=root` is needed because the bot's viz file is `600 root:root` and `/root`
is `700`. The alternatives were changing permissions on the bot's files (you
ruled that out) or ACLs on `/root` (fragile). This matches `v17mm.service`,
which also runs as root. To keep that honest, the unit drops write access at
the kernel level — `ProtectHome=read-only` makes all of `/root` unwritable to
this process, `ProtectSystem=strict` does the same for the rest of the
filesystem. It physically cannot modify the bot's state files.

## Step 0 — point the commands at your host

Every command below uses `$VPS`. The host is deliberately not written down in
this repo — set it in your shell for the session so it never lands in a public
file:

```bash
export VPS=root@your.vps.host
```

## Step 1 — copy the files (no `.git`, no `venv`, no `.env`)

Explicit manifest, not a bulk repo sync. Run from the repo root on your
workstation:

```bash
cd /path/to/dashboard-mm

ssh "$VPS" 'mkdir -p /root/dashboard-mm'

tar czf - \
  server.py config.py requirements.txt \
  adapters/__init__.py adapters/base.py adapters/stats.py \
  adapters/_locking.py \
  adapters/discovery.py adapters/bot_files.py adapters/basis.py \
  adapters/markouts.py adapters/sessions.py \
  adapters/bitunix_account.py adapters/bitunix_klines.py \
  adapters/coinbase_account.py adapters/coinbase_klines.py \
  adapters/okx_account.py adapters/okx_klines.py \
  static/index.html static/app.js static/fill_markers.js static/price_tags.js \
  static/vendor/lightweight-charts.standalone.production.js \
  tests/test_stats.py tests/test_basis.py tests/test_bot_files.py \
  tests/test_discovery.py tests/test_okx_klines.py tests/test_coinbase_klines.py \
  tests/test_okx_account.py tests/test_coinbase_account.py \
  tests/test_bitunix_account.py tests/test_bitunix_klines.py \
  tests/test_markouts.py tests/test_sessions.py \
  tests/test_concurrency.py tests/test_app_js_declarations.py \
  tests/test_stream.py tests/frontend/dom_smoke.js \
  requirements-dev.txt \
  deploy/dashboard-mm.service deploy/healthcheck.sh \
  deploy/dashboard-mm-health.service deploy/dashboard-mm-health.timer \
| ssh "$VPS" 'tar xzf - -C /root/dashboard-mm'
```

The host must be one the bots run on: this dashboard discovers bots by
scanning the local filesystem for the state files they write, so deploying it
anywhere else yields an empty screen. Confirm there is something to find:

```bash
ssh "$VPS" 'ls -l /root/bots/*/*/.*viz*.json /root/.*_tracker/fills.jsonl'
```

Bots are not configured anywhere — whatever those globs match is what shows
up, and a bot started later appears on its own within `DISCOVERY_INTERVAL_S`
(15s by default). Override `VIZ_GLOBS` / `FILLS_GLOBS` if this host lays its
bots out differently.

## Step 2 — venv + dependencies

```bash
ssh "$VPS" '
  cd /root/dashboard-mm &&
  python3 -m venv venv &&
  venv/bin/pip install --quiet --upgrade pip &&
  venv/bin/pip install --quiet -r requirements.txt &&
  venv/bin/python -c "import fastapi, uvicorn, requests, coinbase; print(\"deps ok\")"
'
```

## Step 3 — the API key (you do this, not me)

Create `/root/dashboard-mm/.env` **on the VPS** so the key never passes
through a local shell history or my tool output. In your own terminal:

```
! ssh "$VPS"
```

then on the VPS:

```bash
umask 077
cat > /root/dashboard-mm/.env <<'EOF'
BITUNIX_API_KEY=<your read-only key>
BITUNIX_SECRET_KEY=<your read-only secret>
OKX_API_KEY=<your read-only key>
OKX_SECRET_KEY=<your read-only secret>
OKX_PASSPHRASE=<the passphrase chosen when the OKX key was created>
COINBASE_KEY_FILE=<path to a CDP key file, e.g. the bot's own>
EOF
chmod 600 /root/dashboard-mm/.env
```

Every venue's block is independent — include only the ones you want a
margin panel for. Keys must have trade/withdraw unchecked. This step is
genuinely optional: the unit declares the env file with a leading `-`, so a
missing `.env` is not a startup failure. Skip a venue (or all of them) and
its bots' dashboard rows just show "account margin unavailable"; chart,
fills, positions and order book are unaffected either way. See
`.env.example` for the full set of recognised variables.

## Step 4 — smoke test before installing the service

Runs in the foreground, no systemd, easy to Ctrl-C:

```bash
ssh "$VPS" '
  cd /root/dashboard-mm &&
  set -a && . ./.env 2>/dev/null; set +a
  timeout 15 venv/bin/uvicorn server:app --host 127.0.0.1 --port 8091 &
  sleep 6
  curl -s http://127.0.0.1:8091/snapshot | head -c 600
  echo
'
```

Expect real `orderbook`, `positions`, `klines` values for SOLUSDT. If
`positions` is empty, check the bot is running (`systemctl status v17mm`).

## Step 5 — install and start the service

**This is the step that needs your explicit go-ahead.**

```bash
ssh "$VPS" '
  install -m 644 /root/dashboard-mm/deploy/dashboard-mm.service \
    /etc/systemd/system/dashboard-mm.service &&
  systemctl daemon-reload &&
  systemctl enable --now dashboard-mm.service &&
  sleep 3 &&
  systemctl status dashboard-mm.service --no-pager -l | head -20
'
```

Verify:

```bash
ssh "$VPS" '
  journalctl -u dashboard-mm.service -n 30 --no-pager
  echo "--- local fetch ---"
  curl -s -o /dev/null -w "snapshot http %{http_code}\n" http://127.0.0.1:8091/snapshot
  echo "--- must NOT be reachable externally ---"
  ss -ltnp | grep 8091
'
```

The `ss` line must show `127.0.0.1:8091`, never `0.0.0.0:8091`.

## Step 5b — the health probe

`Restart=on-failure` catches a process that dies. It does not catch uvicorn
staying up while every `/snapshot` returns a 500 or a payload frozen at some
past `server_ts` — the unit stays green and nothing says otherwise. A
oneshot every 2 minutes checks the port answers, the body parses, and
`server_ts` is moving; it exits non-zero otherwise, so the failure shows up
in `systemctl --failed` with the reason in the journal.

```bash
ssh "$VPS" '
  chmod +x /root/dashboard-mm/deploy/healthcheck.sh
  install -m 644 /root/dashboard-mm/deploy/dashboard-mm-health.service /etc/systemd/system/
  install -m 644 /root/dashboard-mm/deploy/dashboard-mm-health.timer  /etc/systemd/system/
  systemctl daemon-reload
  systemctl start dashboard-mm-health.service   # prove it passes before arming it
  systemctl enable --now dashboard-mm-health.timer
  systemctl list-timers dashboard-mm-health.timer --no-pager
'
```

It deliberately does not restart the dashboard: an observer that silently
repairs what it observes hides the incident it exists to surface.

## Step 6 — view it

No port is exposed and there is no auth code. Tunnel from the Mac:

```bash
ssh -N -L 8091:127.0.0.1:8091 "$VPS"
```

Then open <http://127.0.0.1:8091> locally. Close the tunnel to close access.

## Rollback

```bash
ssh "$VPS" '
  systemctl disable --now dashboard-mm.service
  rm -f /etc/systemd/system/dashboard-mm.service
  systemctl daemon-reload
  rm -rf /root/dashboard-mm
'
```

Removes the dashboard completely. Touches nothing belonging to the bot.

## Updating later

Re-run Step 1 (copy) then `systemctl restart dashboard-mm.service`. Static
files (`static/*`) also just need a browser reload — the server reads them
from disk per request.
