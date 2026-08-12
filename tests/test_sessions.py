"""Hand-checked cases for session bounds.

The CSV rows below are copied from this fleet's real
`/root/.v17mm_tracker/sessions_all.csv`, header included, so the parser is
tested against the file it actually has to read rather than an idealised
one.

Runs under pytest, or standalone: `venv/bin/python tests/test_sessions.py`.
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.sessions import (  # noqa: E402
    build_sessions,
    parse_sessions_csv,
    sessions_csv_for,
)

_HEADER = (
    "start,end,duration_s,clean_exit,bx_orders_ok,bx_orders_fail,bx_cancelled,"
    "bx_requoted,cb_placed,cb_failed,cb_edit_ok,cb_edit_fail,fills_total,"
    "fills_bitunix,fills_coinbase,volume_usd,fees_usd,realized_net_usd,"
    "unrealized_usd,round_trips,avg_net_bps,win_rate,median_hold_s,"
    "dd_realized_usd,dd_bx_equity_usd,bx_equity_delta,errors_total"
)
_ROWS = [
    "2026-08-10 22:41:33,2026-08-10 22:56:46,913.0,True,227,169,396,1188,175,3,0,33,"
    "51,12,39,128.71,0.026,-0.3819,-0.8221,57,-59.663,0.7895,145024.3,0.3828,0.02,-0.01,133",
    "2026-08-10 22:56:59,2026-08-11 08:21:19,33860.0,True,519,15903,775,216145,9279,105,0,829,"
    "1918,253,1665,3155.35,0.6415,1.4042,-0.0072,1820,8.552,0.3181,77095.4,0.9011,1.57,-0.65,8642",
]


def _local(s: str) -> float:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp()


def _csv(tmp: Path, rows=None) -> Path:
    p = tmp / "sessions_all.csv"
    p.write_text("\n".join([_HEADER] + (rows if rows is not None else _ROWS)) + "\n")
    return p


def test_parses_real_tracker_rows():
    with tempfile.TemporaryDirectory() as d:
        rows = parse_sessions_csv(_csv(Path(d)))
    assert len(rows) == 2
    start, end, clean = rows[0]
    assert start == _local("2026-08-10 22:41:33")
    assert end == _local("2026-08-10 22:56:46")
    # 913s is what the tracker recorded; the two timestamps must agree with
    # it, which is the check that the parse landed on the right hour.
    assert end - start == 913.0
    assert clean is True


def test_malformed_row_is_skipped_not_raised():
    with tempfile.TemporaryDirectory() as d:
        rows = parse_sessions_csv(
            _csv(Path(d), rows=["not-a-date,also-not,,,", _ROWS[0], ",,,,"])
        )
    assert len(rows) == 1


def test_missing_file_yields_nothing():
    assert parse_sessions_csv(Path("/nonexistent/sessions_all.csv")) == []


def test_csv_located_next_to_its_own_ledger():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        (tmp / "sessions_all.csv").write_text(_HEADER + "\n")
        (tmp / "sessions_okx_all.csv").write_text(_HEADER + "\n")
        assert sessions_csv_for(tmp / "fills.jsonl").name == "sessions_all.csv"
        assert sessions_csv_for(tmp / "fills_okx.jsonl").name == "sessions_okx_all.csv"
        # No session log for this ledger -> None, not a guess at another's.
        assert sessions_csv_for(tmp / "fills_other.jsonl") is None
        assert sessions_csv_for(tmp / "markouts.jsonl") is None


def test_current_session_starts_after_the_last_recorded_one():
    recorded = [(1000.0, 2000.0, True), (3000.0, 4000.0, True)]
    # A fill at 5000 is after the last recorded end -> the bot has been
    # running since, and that fill lower-bounds the start.
    sessions = build_sessions(recorded, first_fill_after=5000.0)
    assert len(sessions) == 3
    cur = sessions[-1]
    assert cur.is_current and cur.end_ts is None
    assert cur.start_ts == 5000.0
    assert cur.start_source == "first fill since last session"
    assert cur.index == 3


def test_observed_start_beats_first_fill():
    # The dashboard watched the bot come back at 4500; its first fill only
    # landed at 5000. The session began at 4500 — the 500s it ran without
    # trading are part of it.
    recorded = [(1000.0, 2000.0, True)]
    cur = build_sessions(recorded, first_fill_after=5000.0, observed_start_ts=4500.0)[-1]
    assert cur.start_ts == 4500.0
    assert cur.start_source == "observed live"


def test_stale_observation_does_not_resurrect_a_finished_session():
    # An observed start from *before* the last recorded session ended is a
    # leftover from an earlier run, not evidence of a current one.
    recorded = [(1000.0, 2000.0, True)]
    sessions = build_sessions(recorded, first_fill_after=None, observed_start_ts=1500.0)
    assert len(sessions) == 1
    assert not sessions[0].is_current


def test_no_evidence_of_a_current_session():
    recorded = [(1000.0, 2000.0, True)]
    # Latest fill predates the last recorded end: the bot is stopped.
    sessions = build_sessions(recorded, first_fill_after=None)
    assert len(sessions) == 1
    assert sessions[0].end_ts == 2000.0


def test_no_tracker_at_all_still_yields_the_current_session():
    # A fleet without the session recorder: everything since the first fill
    # is one session. Degraded, but honest and labelled.
    sessions = build_sessions([], first_fill_after=1234.0)
    assert len(sessions) == 1
    assert sessions[0].is_current and sessions[0].start_ts == 1234.0


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  ok   {name}")
        except AssertionError:
            failures += 1
            print(f"  FAIL {name}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
