"""Only the health authority writes a connection's health fields (P18-1).

Five kinds of writer may change a connection's status (probe, sync run end, credential
lifecycle, operator action, tool-contract ledger) and every one of them reports a
``HealthSignal`` to :class:`~arcagent.extension.connection_health.ConnectionHealthAuthority`,
which alone runs the state machine and the compare-and-set. A sixth writer, patching
``status`` straight into the row, would bypass the transition rules, the notice claim
and the audit, and the card would show whatever it last said.

The store exposes one compare-and-set primitive for the authority; this test is what
keeps it from becoming a back door.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SOURCES = sorted(REPO.glob("packages/*/src/**/*.py"))

AUTHORITY = "packages/arcagent/src/arcagent/extension/connection_health.py"
STORE = "packages/arcagent/src/arcagent/extension/state.py"
#: The store's primitives that move health fields. Only the authority may call them.
HEALTH_PRIMITIVES = re.compile(r"\.(cas_update|compare_and_set)\(")


def _relative(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


def test_the_sources_are_found() -> None:
    assert any(_relative(path) == AUTHORITY for path in SOURCES)


def test_only_the_authority_calls_the_health_primitives() -> None:
    offenders = []
    for path in SOURCES:
        name = _relative(path)
        if name in (AUTHORITY, STORE):
            continue
        text = path.read_text(encoding="utf-8")
        if "ConnectionStateStore" in text and HEALTH_PRIMITIVES.search(text):
            offenders.append(name)

    assert offenders == [], f"these write connection health outside the authority: {offenders}"


def test_nothing_outside_the_store_patches_the_connections_collection() -> None:
    """A direct ``mutable_merge``/``update_if`` on the collection would skip the CAS."""
    offenders = []
    for path in SOURCES:
        name = _relative(path)
        if name == STORE:
            continue
        text = path.read_text(encoding="utf-8")
        if "CONNECTION_COLLECTION" in text and re.search(r"\b(mutable_merge|update_if)\(", text):
            offenders.append(name)

    assert offenders == [], f"these patch the connection rows directly: {offenders}"


def test_the_old_health_writers_are_gone() -> None:
    """``mark_healthy`` and ``set_health`` wrote a status with no rule behind it."""
    for path in SOURCES:
        text = path.read_text(encoding="utf-8")
        assert "def mark_healthy" not in text, _relative(path)
        assert "def set_health" not in text, _relative(path)
