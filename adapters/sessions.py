"""Where one trading session starts and stops.

A session is one run of the bot: it begins when the process starts and ends
when it stops, and the next run starts a fresh one. That is the window an
operator actually reasons in — "how did tonight go", not "how did the last
2000 fills go", which is a window that moves under your feet and silently
splices several runs together.

WHY THIS READS THE TRACKER'S FILES INSTEAD OF DETECTING ANYTHING
---------------------------------------------------------------
This fleet already runs a session recorder alongside each bot
(`v17mm-tracker.service`, started and stopped by the same unit as the bot),
which writes a row per finished session into `sessions*_all.csv` next to the
fill ledger it maintains: start, end, duration, clean_exit, and its own
summary figures. Those bounds are authoritative — they come from the process
lifecycle itself, not from a guess about it.

So this module does not detect sessions. It reads the ones already recorded,
and works out only the one thing the CSV cannot contain: the *current*
session, whose row does not exist yet because it is written on shutdown.

Only the bounds are taken from the CSV. The figures are recomputed by this
dashboard from the fills, deliberately: the tracker's own `realized_net_usd`
comes from a different implementation with different conventions (its OKX
rows carry a negative `fees_usd`, the venue's raw sign), and two different
PnLs for the same session on the same screen would be worse than the problem
this dashboard exists to solve.

TIMEZONE
--------
The CSV timestamps are naive local time, as written by the tracker on the
host. They are parsed as local time, which is correct as long as the
dashboard runs on that same host — it does, that is the only way it can read
these files at all. `sessions_for_ledger` cross-checks the parse against the
ledger anyway (a session with bounds that contain no fills at all is
reported as such), so a timezone mismatch shows up as a visible refusal
rather than as figures quietly attributed to the wrong hours.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_TS_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f")


@dataclass(frozen=True)
class Session:
    index: int  # 1-based, oldest first — what the UI calls "session #N"
    start_ts: float
    end_ts: float | None  # None while the session is still running
    clean_exit: bool | None
    start_source: str  # how start_ts was established; shown in the UI

    @property
    def is_current(self) -> bool:
        return self.end_ts is None


def _parse_ts(raw: str) -> float | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(raw, fmt).timestamp()
        except ValueError:
            continue
    return None


def sessions_csv_for(fills_path: Path) -> Path | None:
    """The tracker's session log that goes with a fill ledger, by the naming
    convention it already uses in that directory:

        fills.jsonl      -> sessions_all.csv
        fills_okx.jsonl  -> sessions_okx_all.csv

    Returns None when there is no such file — a fleet without the tracker
    still gets a dashboard, it just falls back to ledger-inferred bounds.
    """
    stem = fills_path.name
    if not stem.startswith("fills") or not stem.endswith(".jsonl"):
        return None
    suffix = stem[len("fills") : -len(".jsonl")]  # "" or "_okx"
    candidate = fills_path.parent / f"sessions{suffix}_all.csv"
    return candidate if candidate.is_file() else None


def parse_sessions_csv(path: Path) -> list[tuple[float, float, bool | None]]:
    """(start_ts, end_ts, clean_exit) per recorded session, oldest first.

    Tolerant by design, like every other reader here: the tracker owns this
    file's schema and may add columns, so anything unparseable is skipped
    rather than raised — a dashboard that refuses to start because a session
    row is malformed is worse than one missing a row.
    """
    out: list[tuple[float, float, bool | None]] = []
    try:
        with open(path, "r", newline="") as f:
            for row in csv.DictReader(f):
                start = _parse_ts(row.get("start", ""))
                end = _parse_ts(row.get("end", ""))
                if start is None or end is None or end < start:
                    continue
                raw_clean = (row.get("clean_exit") or "").strip().lower()
                clean = True if raw_clean == "true" else (False if raw_clean == "false" else None)
                out.append((start, end, clean))
    except (OSError, csv.Error, UnicodeDecodeError):
        return []
    out.sort(key=lambda s: s[0])
    return out


# A fill this long after the last recorded session's end is treated as
# belonging to a new, still-running session. Generous on purpose: the cost of
# being wrong is only that the current session's start is placed at its first
# fill instead of at the process start.
_CURRENT_SESSION_MIN_GAP_S = 1.0


def build_sessions(
    recorded: list[tuple[float, float, bool | None]],
    first_fill_after: float | None,
    observed_start_ts: float | None = None,
) -> list[Session]:
    """Recorded sessions plus, when there is evidence of one, the current.

    `first_fill_after` is the timestamp of the earliest fill later than the
    last recorded session's end — the ledger's own evidence that the bot has
    been trading since. `observed_start_ts` is when this dashboard actually
    watched the bot come back to life (it runs a liveness thread whether or
    not a browser is open). The observed start is preferred when available
    because it is the real process start; the first fill is only a lower
    bound on it, and both are labelled so the UI can say which one it is
    showing.
    """
    sessions = [
        Session(
            index=i + 1,
            start_ts=start,
            end_ts=end,
            clean_exit=clean,
            start_source="tracker",
        )
        for i, (start, end, clean) in enumerate(recorded)
    ]

    last_end = recorded[-1][1] if recorded else None

    start_ts: float | None = None
    start_source = ""
    if observed_start_ts is not None and (last_end is None or observed_start_ts > last_end):
        start_ts, start_source = observed_start_ts, "observed live"
    elif first_fill_after is not None and (
        last_end is None or first_fill_after > last_end + _CURRENT_SESSION_MIN_GAP_S
    ):
        start_ts, start_source = first_fill_after, "first fill since last session"

    if start_ts is not None:
        sessions.append(
            Session(
                index=len(sessions) + 1,
                start_ts=start_ts,
                end_ts=None,
                clean_exit=None,
                start_source=start_source,
            )
        )
    return sessions
