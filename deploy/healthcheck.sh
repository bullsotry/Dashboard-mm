#!/usr/bin/env bash
# Does the dashboard actually still answer, or is it only "active"?
#
# systemd's Restart=on-failure catches a process that dies. It cannot catch
# the failure mode that actually happened here: uvicorn stays up, the unit
# stays green, and every /snapshot returns a 500 or a frozen payload. Nothing
# reported that, because nothing was asking.
#
# Exits non-zero on failure, so the unit lands in `systemctl --failed` and the
# reason is in the journal. Deliberately does NOT restart anything: an
# observer that silently repairs the thing it observes hides the incident it
# exists to surface.
set -uo pipefail

URL="http://127.0.0.1:${BIND_PORT:-8091}/snapshot"
MAX_AGE_S="${HEALTHCHECK_MAX_AGE_S:-120}"

body=$(mktemp)
trap 'rm -f "$body"' EXIT

# No `|| echo 000` fallback: curl already writes 000 on a failed connection,
# and the fallback would concatenate onto it ("000000").
code=$(curl -s -m 10 -o "$body" -w '%{http_code}' "$URL")
code="${code:-000}"

if [ "$code" != "200" ]; then
  echo "UNHEALTHY: $URL returned HTTP $code (000 = no answer at all)" >&2
  exit 1
fi

# A 200 carrying a payload the frontend cannot use is still a failure. Parse
# it the way the browser would, and check the server's own clock is moving —
# a served-but-frozen snapshot is the case a plain 200 check would pass.
python3 - "$body" "$MAX_AGE_S" <<'PY' >&2 || exit 1
import json, sys, time

path, max_age = sys.argv[1], float(sys.argv[2])
try:
    with open(path) as fh:
        d = json.load(fh)
except (json.JSONDecodeError, OSError) as exc:
    print(f"UNHEALTHY: /snapshot returned 200 but the body is not JSON: {exc}")
    sys.exit(1)

ts = d.get("server_ts")
if not isinstance(ts, (int, float)):
    print("UNHEALTHY: /snapshot has no numeric server_ts")
    sys.exit(1)

age = time.time() - ts
if age > max_age:
    print(f"UNHEALTHY: server_ts is {age:.1f}s old (limit {max_age:.1f}s) — served but frozen")
    sys.exit(1)

# Bot staleness is NOT a dashboard failure: bots stop, and reporting that
# faithfully is the dashboard working. Only say what this check covers.
print(f"healthy: server_ts {age:.1f}s old, {len(d.get('bots') or [])} bot(s) discovered")
PY
