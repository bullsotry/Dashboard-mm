"""Concurrent access to one adapter instance must not corrupt what it read.

/snapshot is a sync `def`, so FastAPI runs it in the anyio threadpool: two
browser tabs, or a forced poll landing on top of a pending one, put several
threads inside the *same* cached adapter at the same time. The background
warm loops add one more. Every one of those threads walks the same tail
offsets and the same memoised replay.

The failure this guards is not a crash — it is a wrong number. Two threads
that both read `_fills_offset` before either writes it back both seek to the
same place and append the same rows, so the FIFO replay prices exits against
inventory that was never bought. That is the exact class of "confident lie"
this repo exists to refuse.

Runs under pytest, or standalone:
`venv/bin/python tests/test_concurrency.py`
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.bot_files import BotStateAdapter  # noqa: E402

N_ROWS = 3000
N_THREADS = 8


def _write_ledger(path: Path, n: int) -> None:
    """n alternating buy/sell fills, so the replay has real lots to match."""
    rows = []
    for i in range(n):
        rows.append(
            {
                "ts": 1_700_000_000.0 + i,
                "venue": "bitunix",
                "symbol": "SOLUSDT",
                "side": "buy" if i % 2 == 0 else "sell",
                "price": 100.0 + (i % 10),
                "size": 1.0,
                "fee": 0.01,
                "contract_value": 1.0,
            }
        )
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _hammer(adapter, fn, n_threads=N_THREADS):
    """Fire n_threads into `fn` released together, so they collide inside
    the tail read rather than politely queueing behind each other."""
    barrier = threading.Barrier(n_threads)
    errors = []

    def run():
        try:
            barrier.wait()
            fn(adapter)
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"threads raised: {errors[:3]}"


def test_concurrent_stats_do_not_duplicate_tailed_fills():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        fills = tmp / "fills.jsonl"
        _write_ledger(fills, N_ROWS)
        viz = tmp / "viz.json"
        viz.write_text(json.dumps({"positions": [], "orderbook": {}}))
        a = BotStateAdapter("bitunix", "SOLUSDT", viz, fills, fills_maxlen=20000)

        _hammer(a, lambda ad: ad.get_stats())

        got = len(a._all_fills())
        assert got == N_ROWS, f"tail read {got} rows from a {N_ROWS}-row ledger"


def test_concurrent_equity_curve_matches_single_threaded():
    """The replay is pure in the fills it reads, so N threads must land on
    the number one thread does. Computed single-threaded first, on its own
    adapter, so the reference cannot be corrupted by the race under test."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        fills = tmp / "fills.jsonl"
        _write_ledger(fills, N_ROWS)
        viz = tmp / "viz.json"
        viz.write_text(json.dumps({"positions": [], "orderbook": {}}))

        reference = BotStateAdapter("bitunix", "SOLUSDT", viz, fills, fills_maxlen=20000)
        expected = reference.get_stats()["realised_net"]

        a = BotStateAdapter("bitunix", "SOLUSDT", viz, fills, fills_maxlen=20000)
        results = []
        lock = threading.Lock()

        def one(ad):
            r = ad.get_stats()["realised_net"]
            with lock:
                results.append(r)

        _hammer(a, one)

        assert all(r == expected for r in results), (
            f"expected {expected}, got {sorted(set(results))}"
        )
        assert len(a._all_fills()) == N_ROWS


def test_concurrent_readers_see_consistent_fill_count():
    """get_recent_fills and get_first_fill_ts walk the same merged list that
    _poll_fills is mutating. A merge racing an append raises rather than
    lying, which is why this asserts no thread raised at all (in _hammer)."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        fills = tmp / "fills.jsonl"
        _write_ledger(fills, N_ROWS)
        viz = tmp / "viz.json"
        viz.write_text(json.dumps({"positions": [], "orderbook": {}}))
        a = BotStateAdapter("bitunix", "SOLUSDT", viz, fills, fills_maxlen=20000)

        def mixed(ad):
            ad.get_first_fill_ts()
            ad.get_recent_fills()
            ad.get_equity_curve()

        _hammer(a, mixed)
        assert len(a._all_fills()) == N_ROWS


if __name__ == "__main__":
    test_concurrent_stats_do_not_duplicate_tailed_fills()
    test_concurrent_equity_curve_matches_single_threaded()
    test_concurrent_readers_see_consistent_fill_count()
    test_concurrent_klines_issue_no_more_fetches_than_one_thread_would()
    test_concurrent_klines_reader_never_sees_a_mutating_dict()
    print("ok")


# ---------------------------------------------------------------------------
# Network-backed adapters: the rule there is different. A lock must never be
# held across a REST call, so the guarantee is not "one at a time" but "one
# in-flight fetch, everyone else serves the cache" — and, above all, no dict
# mutated while another thread walks it.
# ---------------------------------------------------------------------------

from adapters.bitunix_klines import BitunixKlineAdapter  # noqa: E402


class _SlowFetchKlines(BitunixKlineAdapter):
    """Real adapter, fake network: every page takes 200ms, which is what
    makes a duplicate fetch or a mid-iteration mutation actually collide
    instead of finishing too fast to overlap."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fetch_calls = 0
        self._calls_lock = threading.Lock()

    def _fetch_page(self, interval, end_ms):
        with self._calls_lock:
            self.fetch_calls += 1
        time.sleep(0.2)
        # Anchored on now: _materialise drops anything older than the
        # adapter's history window, so fixed 2023 timestamps would
        # materialise to an empty chart and prove nothing.
        base = int(time.time()) // 60 * 60
        if end_ms is not None:
            return []  # no history below the first page: hit the floor at once
        return [
            {
                "time": base - i * 60,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 1.0,
            }
            for i in range(200)
        ]


def test_concurrent_klines_issue_no_more_fetches_than_one_thread_would():
    """8 threads arriving on a cold cache must cost the venue exactly what a
    single thread costs. Without the non-blocking fetch gate each thread runs
    the whole refresh itself — N times the REST traffic, and on a
    rate-limited venue that is how a read-only dashboard gets its own bot's
    IP throttled.

    The reference is measured, not hardcoded: how many pages a cold start
    needs is the adapter's business (recent page, then history until the
    floor), and this test is about concurrency, not about that number.
    """
    reference = _SlowFetchKlines(symbol="SOLUSDT", poll_interval_s=20.0)
    reference.get_klines("1m")
    expected_calls = reference.fetch_calls

    a = _SlowFetchKlines(symbol="SOLUSDT", poll_interval_s=20.0)
    results = []
    lock = threading.Lock()

    def one(ad):
        out = ad.get_klines("1m")
        with lock:
            results.append(len(out))

    _hammer(a, one)

    assert a.fetch_calls == expected_calls, (
        f"{a.fetch_calls} fetches under {N_THREADS} threads where one thread "
        f"needs {expected_calls}"
    )
    # The threads that lost the gate serve the cache as it stood: empty on a
    # genuinely cold start. That is the documented trade — never a blocked
    # request, never a duplicate call — and the winner's bars are there for
    # everyone by the next poll.
    assert max(results) == 200


def test_concurrent_klines_reader_never_sees_a_mutating_dict():
    """Regression guard, and honest about being only that.

    `_materialise` walks `bars` (a list comprehension, then `del`) while a
    merge writes into the same dict — the shape that raises
    RuntimeError('dictionary changed size during iteration'), which on a
    live panel is a 500. But it could NOT be made to fail with the data lock
    removed: 5 runs, 8 threads, even at sys.setswitchinterval(1e-6). Under
    CPython's GIL the interleavings are simply too short to collide here.

    So the data lock in _locking.CacheGuard is defence in depth on this path,
    not a fix for a demonstrated bug — unlike the BotStateAdapter lock above,
    which was measured turning a 1470 PnL into 9798. It stays because it
    costs microseconds and because a free-threaded build removes the very
    property this test relies on. This docstring exists so nobody later
    reads a passing test as proof it was ever needed."""
    a = _SlowFetchKlines(symbol="SOLUSDT", poll_interval_s=0.0)  # always refetch

    def one(ad):
        for _ in range(5):
            ad.get_klines("1m")

    _hammer(a, one)
    assert len(a.get_klines("1m")) == 200
