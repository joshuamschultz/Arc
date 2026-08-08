"""SPEC-064 T-005 — the catalog searches an ordered path, and can list it.

D-584: a bundle resolves from ``<agent>/extensions``, then
``$ARC_EXTENSIONS_ROOT``, then ``<arc_home>/extensions``. Two properties are
what the ordering exists for, and both are asserted rather than assumed:

* **First hit wins by NAME, not by root.** An agent-local ``jira`` overrides the
  fleet's ``jira`` — and every other fleet bundle stays reachable. A search path
  that stopped at the first root holding anything would hide the fleet the
  moment an agent shipped one local bundle.
* **Containment is checked per root.** The symlink guard is not a property of
  root[0]; a bundle in the second root that points outside the second root is
  refused exactly as one in the first is.

:meth:`ExtensionCatalog.available` is the listing every picker will read, so it
must survive a bundle it cannot parse: a broken bundle is REPORTED (with its
reason) and never raises, or one bad directory blanks the whole catalog.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arctrust.audit import AuditEvent

from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier
from arcagent.extension.catalog import (
    ExtensionCatalog,
    resolve_extension_roots,
)


class RecordingSink:
    """Audit sink that keeps every event. ``write`` only — the arctrust protocol."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)

    def actions(self) -> list[str]:
        return [event.action for event in self.events]


def _bundle(root: Path, name: str, *, version: str = "1.0.0", description: str = "") -> Path:
    """Materialise a minimal, parseable bundle directory."""
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    lines = [
        "[extension]",
        f'name = "{name}"',
        f'version = "{version}"',
        'attachment = "cli"',
    ]
    if description:
        lines.append(f'description = "{description}"')
    (folder / "extension.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return folder


def _catalog(*roots: Path, sink: RecordingSink | None = None) -> ExtensionCatalog:
    return ExtensionCatalog(roots=roots, tier=Tier.PERSONAL, audit_sink=sink or RecordingSink())


# --- resolution across the search path ---------------------------------------


def test_a_bundle_in_the_second_root_resolves_when_the_first_lacks_it(tmp_path: Path) -> None:
    """The first root existing is not the end of the search."""
    local = tmp_path / "agent" / "extensions"
    local.mkdir(parents=True)
    fleet = tmp_path / "fleet"
    _bundle(fleet, "jira")

    resolution = _catalog(local, fleet).resolve("jira")

    assert resolution.path == fleet / "jira"


def test_the_earlier_root_wins_by_name_without_hiding_the_later_root(tmp_path: Path) -> None:
    """First hit wins per NAME — the regression the ordering rule exists to prevent.

    An agent-local override of one bundle must not make the rest of the fleet's
    bundles unreachable.
    """
    local = tmp_path / "agent" / "extensions"
    fleet = tmp_path / "fleet"
    _bundle(local, "jira")
    _bundle(fleet, "jira")
    _bundle(fleet, "github")

    catalog = _catalog(local, fleet)

    assert catalog.resolve("jira").path == local / "jira"
    assert catalog.resolve("github").path == fleet / "github"


def test_a_bundle_missing_from_every_root_is_refused_once_and_audited(tmp_path: Path) -> None:
    """``not_found`` is raised only when NO root holds the bundle."""
    first = tmp_path / "first"
    first.mkdir()
    second = tmp_path / "second"
    second.mkdir()
    sink = RecordingSink()

    with pytest.raises(ExtensionError):
        _catalog(first, second, sink=sink).resolve("ghost")

    blocked = [e for e in sink.events if e.action == "extension.blocked"]
    assert [e.extra["reason"] for e in blocked] == ["not_found"]


@pytest.mark.parametrize("escaping_root_index", [0, 1])
def test_containment_refuses_a_symlink_escaping_any_root(
    tmp_path: Path, escaping_root_index: int
) -> None:
    """The symlink guard belongs to EVERY root, not only the first one searched."""
    roots = [tmp_path / "first", tmp_path / "second"]
    for root in roots:
        root.mkdir()
    outside = tmp_path / "outside"
    _bundle(outside, "evil")
    (roots[escaping_root_index] / "evil").symlink_to(outside / "evil", target_is_directory=True)

    sink = RecordingSink()
    with pytest.raises(ExtensionError):
        _catalog(*roots, sink=sink).resolve("evil")

    blocked = next(e for e in sink.events if e.action == "extension.blocked")
    assert blocked.extra["reason"] == "escapes_root"


# --- available() --------------------------------------------------------------


def test_available_lists_across_roots_and_dedupes_by_name(tmp_path: Path) -> None:
    """One entry per name, the earlier root winning, described for a person."""
    local = tmp_path / "agent" / "extensions"
    fleet = tmp_path / "fleet"
    _bundle(local, "jira", version="2.0.0", description="The agent's own Jira.")
    _bundle(fleet, "jira", version="1.0.0", description="The fleet's Jira.")
    _bundle(fleet, "github", description="Read pull requests and issues.")

    entries = _catalog(local, fleet).available()

    by_name = {entry.name: entry for entry in entries}
    assert sorted(by_name) == ["github", "jira"]
    assert by_name["jira"].path == local / "jira"
    assert by_name["jira"].version == "2.0.0"
    assert by_name["jira"].description == "The agent's own Jira."
    assert by_name["github"].description == "Read pull requests and issues."
    assert all(entry.error == "" for entry in entries)


def test_available_reports_an_unparseable_bundle_instead_of_raising(tmp_path: Path) -> None:
    """A broken bundle names its reason and leaves the working ones listed."""
    root = tmp_path / "extensions"
    _bundle(root, "github", description="Read pull requests and issues.")
    broken = root / "broken"
    broken.mkdir()
    (broken / "extension.toml").write_text("[extension]\nname = ", encoding="utf-8")

    entries = {entry.name: entry for entry in _catalog(root).available()}

    assert entries["github"].error == ""
    assert entries["broken"].error != ""
    assert entries["broken"].version == ""


def test_available_ignores_a_directory_that_is_not_a_bundle(tmp_path: Path) -> None:
    """No manifest, no bundle — and no noise in the listing."""
    root = tmp_path / "extensions"
    _bundle(root, "github")
    (root / "notes").mkdir()

    assert [entry.name for entry in _catalog(root).available()] == ["github"]


def test_available_is_empty_when_no_root_exists(tmp_path: Path) -> None:
    """An absent root is not an error — a fresh machine simply has no bundles."""
    assert _catalog(tmp_path / "nowhere").available() == ()


# --- resolve_extension_roots --------------------------------------------------


def test_resolve_extension_roots_orders_agent_then_env_then_arc_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-584's order, with every root that does not exist dropped."""
    agent = tmp_path / "agent"
    (agent / "extensions").mkdir(parents=True)
    env_root = tmp_path / "shared"
    env_root.mkdir()
    home = tmp_path / "arc_home"
    (home / "extensions").mkdir(parents=True)
    monkeypatch.setenv("ARC_EXTENSIONS_ROOT", str(env_root))
    monkeypatch.setenv("ARC_CONFIG_DIR", str(home))

    assert resolve_extension_roots(agent) == (
        agent / "extensions",
        env_root,
        home / "extensions",
    )


def test_resolve_extension_roots_drops_absent_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path that is not there is not searched, and is not an error."""
    home = tmp_path / "arc_home"
    (home / "extensions").mkdir(parents=True)
    monkeypatch.setenv("ARC_CONFIG_DIR", str(home))
    monkeypatch.setenv("ARC_EXTENSIONS_ROOT", str(tmp_path / "missing"))

    assert resolve_extension_roots(tmp_path / "no-such-agent") == (home / "extensions",)


def test_resolve_extension_roots_works_without_an_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """There is a fleet-wide answer: a surface may ask before choosing an agent."""
    home = tmp_path / "arc_home"
    (home / "extensions").mkdir(parents=True)
    monkeypatch.setenv("ARC_CONFIG_DIR", str(home))
    monkeypatch.delenv("ARC_EXTENSIONS_ROOT", raising=False)

    assert resolve_extension_roots() == (home / "extensions",)
