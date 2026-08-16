"""Architecture test — the shipped deployment obeys the documented Arc-home split.

``arctrust.paths`` documents a home split by lifecycle: ``runtime/`` is replaced
wholesale, ``config/`` is preserved, ``state/`` and ``team/`` are never touched,
and runtimes install side by side under ``runtime/<version>/`` behind a
``current`` symlink so an update is an atomic flip.

None of that was true of what actually shipped. The unit ran
``%h/arc/.venv/bin/arc`` — framework code and venv inside a git checkout —
``runtime/current`` was a plain directory holding nothing but ``modules/``, and
the fleet lived at ``%h/arc/team``: durable agent memory inside the disposable
code tree, where every ``git pull`` collided with it. The deploy script and the
unit did not even agree on where the fleet was, and starting from the wrong
empty root loads ZERO agents while reporting healthy.

A docstring cannot hold that line, because nothing reads a docstring at deploy
time. These assertions read the files an operator actually installs and compare
them against the resolver, so the three can never drift apart again.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from arctrust import paths

_REPO = Path(__file__).resolve().parents[2]
_UNIT = _REPO / "deploy" / "systemd" / "arc.service"
_FORWARD_UNIT = _REPO / "deploy" / "systemd" / "arc-connect-forward.service"
_DEPLOY_SCRIPT = _REPO / "scripts" / "deploy-node.sh"

#: systemd's home specifier. Every path in a ``--user`` unit is written against
#: it, so it is the base the accessors are asked to resolve for comparison.
_HOME = "%h"
_ARC_HOME = f"{_HOME}/.arc"

#: Where the framework used to live: a checkout an operator rsyncs and pulls
#: into. Nothing a unit executes, and nothing durable, may sit under it.
_CHECKOUT = f"{_HOME}/arc/"


def _unit_directives(unit: Path) -> dict[str, list[str]]:
    """Return ``{directive: [value, ...]}`` for one systemd unit file."""
    directives: dict[str, list[str]] = {}
    for line in unit.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "[")) or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        directives.setdefault(key.strip(), []).append(value.strip())
    return directives


def _flag_value(command: str, flag: str) -> str:
    """Return the argument following *flag* in a systemd exec line."""
    match = re.search(rf"{re.escape(flag)}\s+(\S+)", command)
    assert match, f"{flag} is not passed at all in: {command}"
    return match.group(1)


@pytest.fixture(scope="module")
def unit() -> dict[str, list[str]]:
    return _unit_directives(_UNIT)


# ---------------------------------------------------------------------------
# The install is the disposable thing
# ---------------------------------------------------------------------------


def test_the_service_runs_the_interpreter_the_runtime_owns(unit: dict[str, list[str]]) -> None:
    """Every exec line names ``runtime/current``'s venv — resolved, not spelled.

    An update replaces ``runtime/<version>/`` and flips the symlink. That is only
    an update of the running code if the running code is what the symlink points
    at; a unit executing a checkout's venv updates on ``git pull`` instead, which
    is neither atomic nor reversible.
    """
    expected = str(paths.runtime_bin("arc", _ARC_HOME))
    for directive in ("ExecStart", "ExecStartPre"):
        for command in unit[directive]:
            assert command.split()[0] == expected, (
                f"{directive} must run {expected} — the runtime's own interpreter"
            )


def test_the_service_works_from_inside_the_active_runtime(unit: dict[str, list[str]]) -> None:
    assert unit["WorkingDirectory"] == [str(paths.arc_runtime(_ARC_HOME))]


@pytest.mark.parametrize("unit_file", [_UNIT, _FORWARD_UNIT], ids=lambda p: p.name)
def test_no_shipped_unit_executes_out_of_a_code_checkout(unit_file: Path) -> None:
    """``%h/arc`` is where operators rsync and pull. Nothing may be run from it.

    This is the assertion that fails first if someone "fixes" a deploy problem by
    pointing a unit back at the checkout, which is how the layout drifted from
    its own documentation the first time.
    """
    directives = _unit_directives(unit_file)
    for key, values in directives.items():
        if not key.startswith(("Exec", "WorkingDirectory", "EnvironmentFile")):
            continue
        for value in values:
            assert _CHECKOUT not in value, (
                f"{unit_file.name}: {key}={value} points into the code checkout"
            )


# ---------------------------------------------------------------------------
# One fleet location, agreed by everything that names it
# ---------------------------------------------------------------------------


def test_the_unit_serves_the_fleet_the_resolver_names(unit: dict[str, list[str]]) -> None:
    """A unit that serves a different root than the resolver serves no agents.

    It does not fail: it starts, reports healthy, and answers with an empty
    fleet. Hardcoding the root in the unit is what made that possible.
    """
    expected = str(paths.arc_team(base=_ARC_HOME))
    for command in unit["ExecStart"] + unit["ExecStartPre"]:
        assert _flag_value(command, "--team-root") == expected


def test_the_unit_reads_the_config_root_the_resolver_names(unit: dict[str, list[str]]) -> None:
    assert unit["EnvironmentFile"] == [str(paths.env_file(_ARC_HOME))]
    gateway = _flag_value(unit["ExecStart"][0], "--gateway-config")
    assert gateway == str(paths.config_file("gateway.toml", _ARC_HOME))


def test_the_deploy_script_asks_the_resolver_where_the_fleet_is() -> None:
    """The script must not carry its own answer — that is what disagreed with the unit.

    ``deploy-node.sh`` defaulted the fleet to one directory while the unit
    hardcoded another. Reading it from ``arc_team()`` makes a second answer
    impossible rather than merely wrong.
    """
    text = _DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assignment = re.search(r"^TEAM_ROOT=(.+)$", text, re.MULTILINE)
    assert assignment, "deploy-node.sh no longer defines TEAM_ROOT"
    assert "arc_team" in assignment.group(1), (
        "TEAM_ROOT must come from arctrust.paths.arc_team(), not from a literal"
    )


def test_the_deploy_script_installs_the_runtime_beside_its_siblings() -> None:
    """A versioned install directory is what makes the flip and the rollback exist."""
    text = _DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assignment = re.search(r"^RUNTIME_DIR=(.+)$", text, re.MULTILINE)
    assert assignment, "deploy-node.sh does not install a versioned runtime"
    assert "$RUNTIME_VERSION" in assignment.group(1)
    assert "activate_runtime" in text or "runtime activate" in text, (
        "nothing in deploy-node.sh points `current` at the version it just installed"
    )


def test_the_deploy_script_never_runs_arc_from_the_checkout() -> None:
    """``$REPO_ROOT/.venv/bin/arc`` is the checkout's venv — the disposable-install bug."""
    text = _DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assignment = re.search(r"^ARC_BIN=(.+)$", text, re.MULTILINE)
    assert assignment, "deploy-node.sh no longer defines ARC_BIN"
    assert "REPO_ROOT" not in assignment.group(1)
    assert "runtime" in assignment.group(1) or "RUNTIME" in assignment.group(1)
