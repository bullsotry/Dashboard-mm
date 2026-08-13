"""Static guard on static/app.js's top-level declarations.

Born from a real outage in this file. `clampBookWidth` ended up declared
twice — an old definition and a new one added further down. Function
declarations hoist, so the *later* definition silently won at the *earlier*
call site, where the `const`s it depended on had not been initialised yet.
That threw `ReferenceError: Cannot access 'FIXED_LAYOUT_OVERHEAD' before
initialization` during page load, and because an uncaught throw abandons the
rest of the script, every feature defined below that line stopped being
wired up: both column-resize handles, the per-panel zoom, the collapse
buttons' siblings. The page still rendered and still polled, so it looked
fine — the controls just quietly did nothing.

`node -c` does not catch this: it is valid syntax. Only running the file
does, and only with the right localStorage state. These two checks catch the
shape of the mistake instead, with no JS runtime needed.

The stronger check — actually loading the page in a DOM and driving the
controls — lives in tests/frontend/dom_smoke.js; see its header. It needs
node + jsdom, so it is not part of this suite.
"""

import re
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parent.parent / "static" / "app.js"

# Top-level declarations only: this file's own convention is that nothing at
# module scope is indented, so a leading non-space is what "top level" means
# here. Anything nested is a different scope and may legitimately shadow.
_TOP_LEVEL_DECL = re.compile(
    r"^(?:async\s+)?(function|const|let|class)\s+([A-Za-z_$][\w$]*)",
    re.MULTILINE,
)


def _declarations():
    return _TOP_LEVEL_DECL.findall(APP_JS.read_text())


def test_no_duplicate_top_level_declarations():
    """Two declarations of one name at module scope is the bug above.

    For `const`/`let` the engine would at least throw a loud SyntaxError.
    For `function` it does not: it hoists, the last one wins everywhere,
    and the failure surfaces far from the edit that caused it.
    """
    seen: dict[str, str] = {}
    duplicates = []
    for kind, name in _declarations():
        if name in seen:
            duplicates.append(f"{name} (as {seen[name]}, then as {kind})")
        seen[name] = kind
    assert not duplicates, "declared twice at top level of app.js: " + ", ".join(duplicates)


def test_const_declared_before_the_functions_that_run_at_load_use_it():
    """A top-level call must not reach a `const` declared below it.

    Worked example, the exact case that broke: `setBookWidth(...)` is called
    at line ~1239 during load; it calls `clampBookWidth`, whose body reads
    `FIXED_LAYOUT_OVERHEAD`. With that const declared at line ~1308, the read
    happens while the binding is in the temporal dead zone -> ReferenceError.
    Moving the const above the first call is the fix, and this asserts it
    stayed there.
    """
    src = APP_JS.read_text()
    lines = src.split("\n")

    def line_of(pattern):
        for i, line in enumerate(lines, 1):
            if re.match(pattern, line):
                return i
        return None

    # The load-time call sites that apply a persisted layout choice.
    first_call = min(
        i for i, line in enumerate(lines, 1)
        if re.match(r"^if \(!Number\.isNaN\(saved(Book|Rail)Width\)\)", line)
    )

    for const_name in ("FIXED_LAYOUT_OVERHEAD", "BOOK_WIDTH_MIN", "RAIL_WIDTH_MIN"):
        decl = line_of(rf"^const {const_name}\b")
        assert decl is not None, f"{const_name} is no longer a top-level const"
        assert decl < first_call, (
            f"{const_name} is declared on line {decl}, after the load-time call on "
            f"line {first_call} that transitively reads it — this is the temporal "
            f"dead zone bug this test exists for"
        )


@pytest.mark.parametrize(
    "name",
    ["clampBookWidth", "clampRailWidth", "setBookWidth", "setRailWidth"],
)
def test_layout_helpers_defined_exactly_once(name):
    """Named explicitly: these four are the ones that actually collided."""
    count = sum(1 for _, decl in _declarations() if decl == name)
    assert count == 1, f"{name} declared {count} times at top level, expected exactly 1"
