"""SPEC-062 T-880 (RED) — ``ExtensionCatalog`` name validation + vetted allowlist.

COMP-002, serving REQ-268. The catalog is the point where an operator-supplied
string becomes a filesystem path and a trust verdict, so it carries two controls,
both lifted from the adapter registry precedent
(``arcgateway/adapters/registry.py``):

* a strict name pattern (``registry.py:57``, ``validate_adapter_name`` at
  ``:79-90``) — ``../evil`` and ``os.system`` never reach the filesystem;
* a vetted-upstream allowlist (``OFFICIAL_ADAPTERS`` at ``:61-65``) with the
  same tier split as ``:240-248`` — an unlisted bundle warns with an audit event
  below federal and is REFUSED at federal.

Every verdict is asserted through the audit sink, not through a return value
alone: a control that decides correctly but records nothing is not a control.
The recorder implements only ``write``, which is the ``arctrust.audit.AuditSink``
protocol — so the catalog must route through the single ``arctrust.audit.emit``
chokepoint (CON-5) rather than poking a sink of its own shape.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arctrust.audit import AuditEvent

from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier
from arcagent.extension.catalog import (
    OFFICIAL_EXTENSIONS,
    ExtensionCatalog,
    validate_extension_name,
)

_OFFICIAL_NAME = "acme_tickets"


class RecordingSink:
    """Audit sink that keeps every event. ``write`` only — the arctrust protocol."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)

    def actions(self) -> list[str]:
        return [event.action for event in self.events]


def _bundle(root: Path, name: str) -> Path:
    """Materialise a minimal bundle directory the catalog can resolve."""
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "extension.toml").write_text(
        f'[extension]\nname = "{name}"\nversion = "1.0.0"\nattachment = "mcp"\n',
        encoding="utf-8",
    )
    return folder


@pytest.fixture
def official_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An extensions root holding one bundle that IS on the vetted allowlist.

    The allowlist entry is added for the duration of the test rather than
    asserting against whatever ships in ``OFFICIAL_EXTENSIONS``, so these tests
    describe the mechanism and not today's shipping roster.
    """
    monkeypatch.setitem(OFFICIAL_EXTENSIONS, _OFFICIAL_NAME, "arcext-acme-tickets")
    _bundle(tmp_path, _OFFICIAL_NAME)
    return tmp_path


@pytest.mark.parametrize(
    "name",
    [
        "../evil",
        "../../etc/passwd",
        "os.system",
        "acme.tickets",
        "acme/tickets",
        "acme\\tickets",
        "acme tickets",
        "acme\ttickets",
        "acme\nrm -rf",
        "Acme",
        "1acme",
        "_acme",
        "",
        "a" * 33,
    ],
    ids=repr,
)
def test_validate_extension_name_refuses_unsafe_names(name: str) -> None:
    """Path separators, dots, whitespace, and case/length violations all refuse."""
    with pytest.raises(ValueError):
        validate_extension_name(name)


@pytest.mark.parametrize("name", ["acme_tickets", "a", "jira", "a" * 32])
def test_validate_extension_name_accepts_safe_names(name: str) -> None:
    """The pattern is ``[a-z][a-z0-9_]{0,31}`` — same shape as the adapter guard."""
    validate_extension_name(name)


def test_resolve_refuses_a_traversal_name_and_audits_the_refusal(official_root: Path) -> None:
    """A traversal name never becomes a path: refusal + a recorded deny."""
    sink = RecordingSink()
    catalog = ExtensionCatalog(roots=[official_root], tier=Tier.PERSONAL, audit_sink=sink)

    with pytest.raises(ExtensionError):
        catalog.resolve("../evil")

    assert "extension.blocked" in sink.actions()
    blocked = next(e for e in sink.events if e.action == "extension.blocked")
    assert blocked.outcome == "deny"
    assert blocked.extra["reason"] == "invalid_name"


def test_resolve_returns_an_official_verdict_for_an_allowlisted_bundle(
    official_root: Path,
) -> None:
    """A vetted bundle resolves to its path and is marked official."""
    sink = RecordingSink()
    catalog = ExtensionCatalog(roots=[official_root], tier=Tier.PERSONAL, audit_sink=sink)

    resolution = catalog.resolve(_OFFICIAL_NAME)

    assert resolution.path == official_root / _OFFICIAL_NAME
    assert resolution.official is True
    assert "extension.unverified" not in sink.actions()


@pytest.mark.parametrize("tier", [Tier.PERSONAL, Tier.ENTERPRISE])
def test_unlisted_bundle_warns_with_an_audit_event_below_federal(
    tmp_path: Path, tier: Tier
) -> None:
    """Below federal an unvetted bundle loads — but the risk is recorded, allow-with-warning."""
    _bundle(tmp_path, "rando_connector")
    sink = RecordingSink()
    catalog = ExtensionCatalog(roots=[tmp_path], tier=tier, audit_sink=sink)

    resolution = catalog.resolve("rando_connector")

    assert resolution.official is False
    assert "extension.unverified" in sink.actions()
    unverified = next(e for e in sink.events if e.action == "extension.unverified")
    assert unverified.outcome == "allow"
    assert unverified.extra["reason"] == "not_official"
    assert unverified.target.endswith("rando_connector")


def test_unlisted_bundle_is_refused_at_federal(tmp_path: Path) -> None:
    """Federal is a signed-allowlist control point: unlisted means unloadable."""
    _bundle(tmp_path, "rando_connector")
    sink = RecordingSink()
    catalog = ExtensionCatalog(roots=[tmp_path], tier=Tier.FEDERAL, audit_sink=sink)

    with pytest.raises(ExtensionError):
        catalog.resolve("rando_connector")

    blocked = next(e for e in sink.events if e.action == "extension.blocked")
    assert blocked.outcome == "deny"
    assert blocked.extra["reason"] == "not_official"


def test_allowlisted_bundle_still_resolves_at_federal(official_root: Path) -> None:
    """Federal refuses the UNLISTED bundle, not every bundle."""
    sink = RecordingSink()
    catalog = ExtensionCatalog(roots=[official_root], tier=Tier.FEDERAL, audit_sink=sink)

    resolution = catalog.resolve(_OFFICIAL_NAME)

    assert resolution.official is True


def test_missing_bundle_is_refused_and_audited(official_root: Path) -> None:
    """A name that passes validation but has no bundle on disk fails closed."""
    sink = RecordingSink()
    catalog = ExtensionCatalog(roots=[official_root], tier=Tier.PERSONAL, audit_sink=sink)

    with pytest.raises(ExtensionError):
        catalog.resolve("ghost_connector")

    assert "extension.blocked" in sink.actions()


def test_resolve_never_escapes_the_extensions_root(tmp_path: Path) -> None:
    """The resolved path stays inside the root even for a name that survives validation.

    Belt-and-braces on the traversal guard: validation is the first defence, and
    containment of the resolved path is the second.
    """
    root = tmp_path / "extensions"
    root.mkdir()
    _bundle(root, "rando_connector")
    outside = tmp_path / "outside"
    outside.mkdir()

    sink = RecordingSink()
    catalog = ExtensionCatalog(roots=[root], tier=Tier.PERSONAL, audit_sink=sink)
    resolution = catalog.resolve("rando_connector")

    assert root.resolve() in resolution.path.resolve().parents
