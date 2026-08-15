"""The NAV curve must be sampled on a clock, not on attention.

Sampling used to happen inside `_bot_summary`, which runs once per snapshot
build. That made the curve's x-axis "however often somebody happened to be
looking": twice as fast with two tabs open, faster again under /stream, and
nothing at all while no tab was open. A chart whose sample rate depends on
who is watching states a window it does not have — the same class of
confident lie this repo refuses everywhere else.

It also broke /stream outright: a snapshot that grew a NAV point on every
build differed from the previous one by construction, so nothing was ever
suppressed and the whole payload was resent every 0.5s.

Guarding the invariant by shape rather than by behaviour is deliberate.
Reproducing it at runtime needs a discovered bot, a state adapter and an
account adapter all stubbed into module globals; the property is simply
"only the warm loop writes here", and that is exactly what the AST says.

Runs under pytest, or standalone:
`venv/bin/python tests/test_nav_sampling.py`
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Where a write to _nav_history is legitimate.
#   _account_warm_loop — the fixed-cadence sampler, the whole point.
#   _refresh           — drops history for bots that have disappeared.
ALLOWED_WRITERS = {"_account_warm_loop", "_refresh"}


def _functions_writing_nav_history() -> set[str]:
    tree = ast.parse((ROOT / "server.py").read_text())
    writers: set[str] = set()

    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            # _nav_history.pop(...) / .setdefault(...) / .clear(...)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "_nav_history"
                and node.func.attr in {"pop", "setdefault", "clear", "update"}
            ):
                writers.add(fn.name)
            # _nav_history[key] = ...
            for target in getattr(node, "targets", []):
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "_nav_history"
                ):
                    writers.add(fn.name)
    return writers


def test_only_the_warm_loop_samples_nav():
    writers = _functions_writing_nav_history()
    unexpected = writers - ALLOWED_WRITERS
    assert not unexpected, (
        f"{sorted(unexpected)} writes to _nav_history. Sampling belongs in "
        "_account_warm_loop, on a stated cadence — anywhere on the request "
        "path makes the curve's sample rate depend on who is watching"
    )


def test_the_warm_loop_really_does_sample():
    """The mirror image: if the sampler were deleted, the check above would
    pass with an empty set and the curve would silently never fill."""
    assert "_account_warm_loop" in _functions_writing_nav_history(), (
        "nothing samples the NAV curve any more"
    )


def test_bot_summary_stays_read_only():
    """Named explicitly because this is where it was, and where a future
    change would most naturally put it back."""
    assert "_bot_summary" not in _functions_writing_nav_history()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
    print("ALL CHECKS PASSED")
