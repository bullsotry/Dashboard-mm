"""Thread-safety plumbing shared by the adapters that cache a network fetch.

Not a venue concern — pure mechanism — so it lives here rather than being
copied into six adapter modules, where six chances to get it subtly wrong is
exactly one chance too many.

The constraint that shapes this: **a lock must never be held across a network
call.** A kline backfill is up to 8 pages at a 5s timeout; a plain mutex
around the whole refresh would make a concurrent request wait up to 40s for
data it already has cached, which is the very stall the background warm loops
exist to remove. So there are two separate things here:

- `data` — a reentrant lock held only for dict reads/merges (microseconds).
- `try_fetch()` — a *non-blocking* gate. Whoever holds it is doing the
  network I/O; anyone else who wants it simply serves the cache it already
  has instead of queueing behind a fetch or firing a duplicate one.

The result: at most one in-flight fetch per adapter, no thread ever blocked
on the network, and no dict mutated while another thread iterates it.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager


class CacheGuard:
    def __init__(self) -> None:
        # Reentrant: a guarded method may call another one (materialise from
        # inside a refresh, for instance).
        self.data = threading.RLock()
        self._fetching = threading.Lock()

    @contextmanager
    def try_fetch(self):
        """Yields True to exactly one thread at a time, False to the rest.

        A False is not an error and must not be retried in a loop — it means
        another thread is already fetching this same data, so the correct
        response is to serve the current cache and let the poll interval come
        round again.
        """
        acquired = self._fetching.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                self._fetching.release()
