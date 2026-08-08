"""``arc connector available`` — what can this deployment connect to?

SPEC-064 T-011. The other eight verbs all take an extension name the operator
must already know, which makes "what can I connect?" a question the terminal
could not answer and a picker no surface could draw.

Three properties are the point of the verb:

* **It works without ``--agent``.** There is a fleet-wide answer (D-584's
  ``$ARC_EXTENSIONS_ROOT`` and ``<arc_home>/extensions``), and an operator
  choosing which agent to connect has not chosen one yet.
* **``--agent`` adds that agent's own bundles**, ahead of the fleet's, because
  that is the resolution order every other verb uses.
* **An unreadable bundle is shown with its reason.** A directory that would not
  parse is precisely what an operator needs told; dropping it silently turns a
  broken bundle into a missing one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arccli.commands.connector import _SUBCOMMAND_MAP, connector_handler

_AGENT_CONFIG = (
    "[agent]\n"
    'name = "sales_agent"\n\n'
    "[identity]\n"
    'did = "did:arc:local:executor/7e3e"\n\n'
    "[security]\n"
    'tier = "personal"\n'
)


def _bundle(root: Path, name: str, *, version: str = "1.0.0", description: str = "") -> Path:
    """A minimal bundle the shipped manifest parser accepts."""
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
    lines += ["", "[tools]", 'allow = ["noop"]', "", "[config.cli]", 'binary = "noop"']
    (folder / "extension.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return folder


@pytest.fixture
def fleet_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fleet-wide ``<arc_home>/extensions`` holding one bundle."""
    home = tmp_path / "arc_home"
    root = home / "extensions"
    _bundle(root, "github", description="Read pull requests, issues, and CI runs.")
    monkeypatch.setenv("ARC_CONFIG_DIR", str(home))
    monkeypatch.delenv("ARC_EXTENSIONS_ROOT", raising=False)
    return root


@pytest.fixture
def agent_dir(tmp_path: Path) -> Path:
    """An agent home with one bundle of its own."""
    agent = tmp_path / "sales_agent"
    agent.mkdir()
    (agent / "arcagent.toml").write_text(_AGENT_CONFIG, encoding="utf-8")
    _bundle(agent / "extensions", "acme_tickets", description="Open and read Acme tickets.")
    return agent


def test_available_is_a_reachable_verb() -> None:
    """A ninth verb on a command that is already registered."""
    assert "available" in _SUBCOMMAND_MAP


def test_lists_the_fleet_bundles_without_an_agent(
    fleet_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No ``--agent``: the fleet-wide search path still has an answer."""
    connector_handler(["available"])

    out = capsys.readouterr().out
    assert "github" in out
    assert "Read pull requests, issues, and CI runs." in out


def test_an_agents_own_bundles_are_included_with_the_fleets(
    fleet_root: Path, agent_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--agent`` searches that agent's extensions dir ahead of the fleet's."""
    connector_handler(["available", "--agent", str(agent_dir)])

    out = capsys.readouterr().out
    assert "acme_tickets" in out
    assert "github" in out


def test_json_reports_name_version_description_and_source(
    fleet_root: Path, agent_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--json`` is the shape a picker reads; the source root is part of it."""
    connector_handler(["available", "--agent", str(agent_dir), "--json"])

    payload = json.loads(capsys.readouterr().out)
    entries = {entry["name"]: entry for entry in payload}
    assert entries["acme_tickets"]["version"] == "1.0.0"
    assert entries["acme_tickets"]["description"] == "Open and read Acme tickets."
    assert entries["acme_tickets"]["path"] == str(agent_dir / "extensions" / "acme_tickets")
    assert entries["github"]["path"] == str(fleet_root / "github")
    assert entries["github"]["error"] == ""


def test_an_unreadable_bundle_is_listed_with_its_reason(
    fleet_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A bundle that will not parse is reported, never silently dropped."""
    broken = fleet_root / "broken"
    broken.mkdir()
    (broken / "extension.toml").write_text("[extension]\nname = ", encoding="utf-8")

    connector_handler(["available", "--json"])

    entries = {entry["name"]: entry for entry in json.loads(capsys.readouterr().out)}
    assert entries["broken"]["error"] != ""
    assert entries["github"]["error"] == ""


def test_says_so_when_no_bundle_is_reachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An empty search path is reported with the roots searched, not as a failure."""
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "empty_home"))
    monkeypatch.delenv("ARC_EXTENSIONS_ROOT", raising=False)

    connector_handler(["available"])

    assert "No extension bundles" in capsys.readouterr().out
