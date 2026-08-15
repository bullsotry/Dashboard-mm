"""/stream must push what changed, stay quiet about what did not, and never
go silently mute.

Three properties, each of which failing produces a dashboard that looks
healthy while lying:

- A payload whose only difference is `server_ts` is not a change. If it were,
  the stream would resend the whole screen every tick and be a poll with
  extra steps.
- Silence must be distinguishable from a dead link. The frontend's "link
  down" badge fires after LINK_STALE_MS with no word from the server; under
  a push transport, silence is the *normal* state of a healthy link, so the
  heartbeat is the only thing standing between a quiet market and a red
  badge on a perfectly good connection.
- The stream must not be gzipped. GzipFile buffers until it has enough input
  or is closed, and this response never closes — a compressed stream can
  connect successfully and then deliver nothing.

These drive the route's own async generator rather than going through
Starlette's TestClient. That is not a shortcut: TestClient runs the whole
app to completion into a BytesIO before it returns a response object, so
`client.stream()` cannot read an endless response at all — it just hangs.
The generator is the thing under test anyway.

Runs under pytest, or standalone:
`venv/bin/python tests/test_stream.py`
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Set before importing config/server: both read their intervals at import.
# Short enough that the whole file runs in about a second.
os.environ.setdefault("STREAM_INTERVAL_S", "0.02")
os.environ.setdefault("STREAM_HEARTBEAT_S", "0.15")
# Point discovery at a directory with no bots in it, so nothing on the host
# running these tests can leak into the assertions.
os.environ.setdefault("VIZ_GLOBS", "/nonexistent-for-tests/*.json")
os.environ.setdefault("FILLS_GLOBS", "/nonexistent-for-tests/*.jsonl")

import server  # noqa: E402


class _FakeRequest:
    """Only `is_disconnected` is ever touched by the route."""

    def __init__(self) -> None:
        self.disconnected = False

    async def is_disconnected(self) -> bool:
        return self.disconnected


def _parse(frame: str) -> tuple[str, str]:
    name = data = ""
    for line in frame.strip().splitlines():
        if line.startswith("event: "):
            name = line[len("event: ") :]
        elif line.startswith("data: "):
            data = line[len("data: ") :]
    return name, data


def _collect(n: int, timeout_s: float = 5.0, on_frame=None) -> list[tuple[str, str]]:
    """Pull the first n SSE frames out of the route's generator.

    `on_frame(i)` runs between frames, which is how a test mutates the world
    mid-stream and asserts the change is noticed.
    """

    async def run():
        request = _FakeRequest()
        response = await server.stream(request)
        out: list[tuple[str, str]] = []
        deadline = time.time() + timeout_s
        async for chunk in response.body_iterator:
            out.append(_parse(chunk))
            if on_frame:
                on_frame(len(out))
            if len(out) >= n:
                request.disconnected = True
                break
            if time.time() > deadline:
                raise AssertionError(f"only {len(out)} frame(s) in {timeout_s}s")
        return out

    return asyncio.run(run())


def _with_stub(stub, fn):
    original = server._build_snapshot
    server._build_snapshot = stub
    try:
        return fn()
    finally:
        server._build_snapshot = original


def test_server_ts_alone_is_not_a_change():
    """The whole economy of the stream rests on this. A stub returning an
    identical payload except for its clock must produce exactly one snapshot,
    then heartbeats — never a second snapshot."""
    calls = {"n": 0}

    def stub(bot=None, interval=None, ksig=None, session=None):
        calls["n"] += 1
        return {"server_ts": time.time(), "bot": "b", "bots": [], "venue": None}

    events = _with_stub(stub, lambda: _collect(3))

    assert calls["n"] > 3, "the stream stopped rebuilding"
    kinds = [name for name, _ in events]
    assert kinds[0] == "snapshot", f"first frame must be the state, got {kinds}"
    assert kinds[1:] == ["heartbeat", "heartbeat"], (
        f"an unchanged screen must produce heartbeats, not resends: {kinds}"
    )


def test_a_real_change_is_pushed():
    """The mirror image: when something the operator would see changes, it
    arrives as a snapshot rather than being swallowed as 'unchanged'."""
    state = {"equity": 100.0}

    def stub(bot=None, interval=None, ksig=None, session=None):
        return {
            "server_ts": time.time(),
            "bot": "b",
            "bots": [],
            "venue": {"account": {"equity": state["equity"]}},
        }

    def bump(i):
        if i == 1:
            state["equity"] = 101.0

    events = _with_stub(stub, lambda: _collect(2, on_frame=bump))

    assert events[0][0] == "snapshot" and "100.0" in events[0][1]
    assert events[1][0] == "snapshot", f"a changed equity must be pushed, got {events}"
    assert "101.0" in events[1][1]


def test_candles_are_sent_once_then_suppressed():
    """Candles are ~93% of the payload. The server feeds the signature it
    last *sent* back into the next build, so a second frame carries
    klines=None ('unchanged, keep what you have') instead of the history
    again — and must not record a signature for a history it never sent."""
    seen_ksig = []

    def stub(bot=None, interval=None, ksig=None, session=None):
        seen_ksig.append(ksig)
        klines = None if ksig == "sig-1" else [[1, 2, 3]]
        return {
            "server_ts": time.time(),
            "bot": "b",
            "bots": [],
            # `n` forces a fresh fingerprint each build, so suppression is
            # what is being measured here and not the change detector.
            "n": len(seen_ksig),
            "venue": {"klines": klines, "klines_sig": "sig-1"},
        }

    events = _with_stub(stub, lambda: _collect(3))

    assert seen_ksig[0] is None, "first build must not claim to hold a history"
    assert seen_ksig[1] == "sig-1", "the sent signature must be fed back"
    assert '"klines": [[1, 2, 3]]' in events[0][1].replace(",", ", ") or "1, 2, 3" in events[0][1]
    assert '"klines":null' in events[1][1].replace(" ", ""), (
        f"second frame must suppress the candles: {events[1][1][:200]}"
    )


def test_stream_is_exempt_from_compression():
    """A gzipped stream can connect and then deliver nothing at all — the
    compressor holds the frames.

    The discriminant is which `send` the inner app is handed: bypassed, it
    gets the original object; compressed, GZipMiddleware substitutes its own
    wrapper. Asserting on that is what makes this a test rather than a
    restatement of the code.
    """
    got = {}

    async def inner_app(scope, receive, send):
        got["send"] = send

    mw = server._GZipExceptStream(inner_app)
    sentinel = object()
    gzip_headers = [(b"accept-encoding", b"gzip")]

    async def call(path):
        got.clear()
        await mw({"type": "http", "path": path, "headers": gzip_headers}, None, sentinel)
        return got.get("send")

    assert asyncio.run(call("/stream")) is sentinel, (
        "/stream must reach the app with the untouched send — anything else "
        "means a compressor sits between the generator and the socket"
    )
    assert asyncio.run(call("/snapshot")) is not sentinel, (
        "every other path must still be compressed; /snapshot is 93% candles"
    )


def test_fingerprint_ignores_only_the_clock():
    """Guards the exclusion list itself: `server_ts` is skipped, everything
    else counts. A fingerprint that quietly ignored a real field would show
    up here as two different payloads hashing the same."""
    base = {"server_ts": 1.0, "bot": "b", "venue": {"account": {"equity": 100.0}}}
    later = {"server_ts": 999.0, "bot": "b", "venue": {"account": {"equity": 100.0}}}
    changed = {"server_ts": 1.0, "bot": "b", "venue": {"account": {"equity": 100.5}}}

    assert server._payload_fingerprint(base) == server._payload_fingerprint(later)
    assert server._payload_fingerprint(base) != server._payload_fingerprint(changed)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
    print("ALL CHECKS PASSED")
