#!/usr/bin/env bash
# Local/dev runner. On the VPS, the systemd unit uses EnvironmentFile=.env
# directly (same pattern as v17mm.service) instead of this script.
set -euo pipefail
cd "$(dirname "$0")"
if [ -f .env ]; then
  set -a
  source .env
  set +a
fi
exec venv/bin/uvicorn server:app --host "${BIND_HOST:-127.0.0.1}" --port "${BIND_PORT:-8091}"
