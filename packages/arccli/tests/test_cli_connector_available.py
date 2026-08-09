"""``arc connector available`` — what can this deployment connect to?

SPEC-064 T-011. The other eight verbs all take an extension name the operator
must already know, which makes "what can I connect?" a question the terminal
could not answer and a picker no surface could draw.

Three properties are the point of the verb:

* **It works with no flags at all.** There is a deployment-wide answer (D-584's
  ``$ARC_EXTENSIONS_ROOT`` and ``<arc_home>/extensions``), and an operator
  asking what could be connected has chosen nothing yet.
* **``--arc-dir`` adds that deployment's own bundles**, ahead of the user-wide
  ones, because that is the resolution order every other verb uses. There is
  deliberately no per-agent root: a connection is the deployment's, so a bundle
  only one agent could resolve would be a grant that works by accident.
* **An unreadable bundle is shown with its reason.** A directory that would not
  parse is precisely what an operator needs told; dropping it silently turns a
  broken bundle into a missing one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arccli.commands.connector import _SUBCOMMAND_MAP, connector_handler


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
def local_arc_dir(tmp_path: Path) -> Path:
    """A second deployment root with one bundle of its own."""
    root = tmp_path / "local_arc"
    _bundle(root / "extensions", "acme_tickets", description="Open and read Acme tickets.")
    return root


def test_available_is_a_reachable_verb() -> None:
    """A verb on a command that is already registered."""
    assert "available" in _SUBCOMMAND_MAP


def test_lists_the_user_wide_bundles_with_no_flags(
    fleet_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No flags: the user-wide search path still has an answer."""
    connector_handler(["available"])

    out = capsys.readouterr().out
    assert "github" in out
    assert "Read pull requests, issues, and CI runs." in out


def test_a_deployments_own_bundles_are_included_with_the_user_wide_ones(
    fleet_root: Path, local_arc_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--arc-dir`` searches that deployment's extensions dir first."""
    connector_handler(["available", "--arc-dir", str(local_arc_dir)])

    out = capsys.readouterr().out
    assert "acme_tickets" in out
    assert "github" in out


def test_json_reports_name_version_description_and_source(
    fleet_root: Path, local_arc_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--json`` is the shape a picker reads; the source root is part of it."""
    connector_handler(["available", "--arc-dir", str(local_arc_dir), "--json"])

    payload = json.loads(capsys.readouterr().out)
    entries = {entry["name"]: entry for entry in payload}
    assert entries["acme_tickets"]["version"] == "1.0.0"
    assert entries["acme_tickets"]["description"] == "Open and read Acme tickets."
    assert entries["acme_tickets"]["path"] == str(local_arc_dir / "extensions" / "acme_tickets")
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
