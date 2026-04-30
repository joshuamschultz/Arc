"""Static guard — arcui has zero direct filesystem access to ``team/``.

This is the structural enforcement of SPEC-022 acceptance criterion 16:

    arcui has zero direct filesystem access to ``team/`` — verified by static
    grep test that no arcui module imports ``pathlib`` / ``os.path`` to touch
    ``team/`` and that ``watchfiles`` is never imported anywhere in arcui.

If this test fails, **do not edit the regex to silence it**. arcui must reach
``team/`` only through ``arcgateway.fs_reader``. The only path that should
ever appear in this repo's arcui sources is the import of the gateway module
itself — that import is correct, the literal ``team/`` is wrong.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# Repo path: packages/arcui/src/arcui/
_ARCUI_SRC = Path(__file__).resolve().parents[1] / "src" / "arcui"

# Patterns that prove a module is reaching ``team/`` directly.
#
# 1. Path('team/...')           — pathlib literal targeting team
# 2. Path("team/...")
# 3. os.path.join("team", ...)
# 4. open("team/...")
# 5. import watchfiles          — watchfiles is gateway-only by D-012
# 6. from watchfiles import ... — same rule, alternative form
_FORBIDDEN = [
    re.compile(r"""Path\(\s*['"]team/"""),
    re.compile(r"""os\.path\.[a-z_]+\(\s*['"]team['"]"""),
    re.compile(r"""os\.path\.[a-z_]+\([^)]*['"]team/"""),
    re.compile(r"""open\(\s*['"]team/"""),
    re.compile(r"""^\s*import\s+watchfiles\b""", re.MULTILINE),
    re.compile(r"""^\s*from\s+watchfiles\s+import\b""", re.MULTILINE),
]


def _python_sources() -> list[Path]:
    return [p for p in _ARCUI_SRC.rglob("*.py") if p.is_file()]


@pytest.mark.parametrize("pattern_idx", range(len(_FORBIDDEN)))
def test_no_forbidden_pattern(pattern_idx: int) -> None:
    """No arcui source file may match any forbidden pattern."""
    pattern = _FORBIDDEN[pattern_idx]
    offenders: list[tuple[Path, int, str]] = []
    for path in _python_sources():
        text = path.read_text(encoding="utf-8")
        for line_num, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                offenders.append((path, line_num, line.strip()))
    if offenders:
        rendered = "\n".join(
            f"  {p.relative_to(_ARCUI_SRC.parent.parent)}:{ln}: {snippet}"
            for p, ln, snippet in offenders
        )
        raise AssertionError(
            "arcui must reach team/ only through arcgateway.fs_reader.\n"
            f"Pattern {pattern.pattern!r} matched:\n{rendered}"
        )


def test_no_watchfiles_anywhere() -> None:
    """Belt-and-suspenders: no arcui file mentions ``watchfiles`` at all."""
    offenders = []
    for path in _python_sources():
        text = path.read_text(encoding="utf-8")
        if "watchfiles" in text:
            offenders.append(path)
    assert not offenders, (
        "arcui must not import or reference watchfiles. "
        f"Offending files: {[str(p) for p in offenders]}"
    )


def test_arcui_imports_arcgateway_for_data_access() -> None:
    """Sanity check: at least one arcui module imports arcgateway data plane."""
    found = False
    for path in _python_sources():
        text = path.read_text(encoding="utf-8")
        if "from arcgateway import" in text or "import arcgateway" in text:
            found = True
            break
    assert found, (
        "Expected at least one arcui module to import arcgateway "
        "(routes/agent_detail.py and routes/team_pages.py do)."
    )
