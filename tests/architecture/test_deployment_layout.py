"""Architecture test — the shipped deployment obeys the documented layout.

``arctrust.paths`` documents a home split by lifecycle: ``runtime/`` is replaced
wholesale, ``config/`` is preserved, ``state/`` is never touched, and runtimes
install side by side under ``runtime/<version>/`` behind a ``current`` symlink so
an update is an atomic flip. The fleet is a directory further out again, at
``~/arc/team``, so that replacing the whole of ``~/.arc`` cannot reach it.

None of that was true of what actually shipped. The unit ran
``%h/arc/.venv/bin/arc`` — framework code and venv inside a git checkout —
``runtime/current`` was a plain directory holding nothing but ``modules/``, and
the deploy script and the unit did not agree on where the fleet was. Starting
from the wrong empty root loads ZERO agents while reporting healthy.

A docstring cannot hold that line, because nothing reads a docstring at deploy
time. These assertions read the files an operator actually installs and resolve
them through the accessors, under the same environment systemd gives the
service, so the three can never drift apart again.
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

#: systemd's home specifier. A ``--user`` unit writes every path against it, and
#: sets neither ARC_CONFIG_DIR nor ARC_TEAM_ROOT — so standing HOME up as ``%h``
#: with both unset makes the accessors answer exactly what the service will
#: resolve at runtime. Comparing against that is stronger than comparing against
#: a spelled-out string: it is the same code path the process takes.
_HOME = "%h"


@pytest.fixture
def service_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve accessors the way the systemd unit's process will."""
    monkeypatch.setenv("HOME", _HOME)
    monkeypatch.delenv("ARC_CONFIG_DIR", raising=False)
    monkeypatch.delenv("ARC_TEAM_ROOT", raising=False)


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


@pytest.fixture
def unit() -> dict[str, list[str]]:
    return _unit_directives(_UNIT)


def _script() -> str:
    return _DEPLOY_SCRIPT.read_text(encoding="utf-8")


def _assignment(name: str) -> str:
    """Return the right-hand side of a top-level ``NAME=...`` line in the script."""
    match = re.search(rf"^{name}=(.+)$", _script(), re.MULTILINE)
    assert match, f"deploy-node.sh no longer defines {name}"
    return match.group(1)


# ---------------------------------------------------------------------------
# The install is the disposable thing
# ---------------------------------------------------------------------------


def test_the_service_runs_the_interpreter_the_runtime_owns(
    unit: dict[str, list[str]], service_env: None
) -> None:
    """Every exec line names ``runtime/current``'s venv — resolved, not spelled.

    An update replaces ``runtime/<version>/`` and flips the symlink. That is only
    an update of the running code if the running code is what the symlink points
    at; a unit executing a checkout's venv updates on ``git pull`` instead, which
    is neither atomic nor reversible.
    """
    expected = str(paths.runtime_bin("arc"))
    for directive in ("ExecStart", "ExecStartPre"):
        for command in unit[directive]:
            assert command.split()[0] == expected, (
                f"{directive} must run {expected} — the runtime's own interpreter"
            )


def test_the_service_works_from_inside_the_active_runtime(
    unit: dict[str, list[str]], service_env: None
) -> None:
    assert unit["WorkingDirectory"] == [str(paths.arc_runtime())]


@pytest.mark.parametrize("unit_file", [_UNIT, _FORWARD_UNIT], ids=lambda p: p.name)
def test_no_shipped_unit_executes_out_of_the_checkout(unit_file: Path, service_env: None) -> None:
    """Nothing may be RUN from the directory operators rsync and pull into.

    This is the assertion that fails first if someone "fixes" a deploy problem by
    pointing a unit back at the checkout, which is how the layout drifted from
    its own documentation the first time.

    Data is a different matter and belongs there: the fleet, the config and the
    state all live under the operator root by design, so that replacing the
    install home costs nothing. A unit names those as data — an EnvironmentFile
    is read, never executed — and only the directives that start a process are
    held to the checkout rule.
    """
    checkout = f"{paths.arc_team().parent}/"
    directives = _unit_directives(unit_file)
    for key, values in directives.items():
        if not key.startswith(("Exec", "WorkingDirectory")):
            continue
        for value in values:
            executable = value.split()[0] if key.startswith("Exec") else value
            assert not executable.startswith(checkout), (
                f"{unit_file.name}: {key} executes out of the code checkout: {executable}"
            )


# ---------------------------------------------------------------------------
# One fleet location, agreed by everything that names it
# ---------------------------------------------------------------------------


def test_the_unit_serves_the_fleet_the_resolver_names(
    unit: dict[str, list[str]], service_env: None
) -> None:
    """A unit that serves a different root than the resolver serves no agents.

    It does not fail: it starts, reports healthy, and answers with an empty
    fleet. Hardcoding a root in the unit that nothing else agreed to is exactly
    what made that possible.
    """
    expected = str(paths.arc_team())
    for command in unit["ExecStart"] + unit["ExecStartPre"]:
        assert _flag_value(command, "--team-root") == expected


def test_the_fleet_survives_replacing_the_whole_arc_home(service_env: None) -> None:
    """``rm -rf ~/.arc`` is the move an operator reaches for. It must cost nothing.

    Dropping a fresh install in is the documented update story, so the home has
    to be disposable in the strongest sense — and that is only true while no
    agent's memory, identity, tools, skills or workspace is underneath it.
    """
    assert paths.arc_home() not in paths.arc_team().parents
    assert paths.arc_runtime_root() not in paths.arc_team().parents


def test_the_unit_reads_the_config_root_the_resolver_names(
    unit: dict[str, list[str]], service_env: None
) -> None:
    assert unit["EnvironmentFile"] == [str(paths.env_file())]
    gateway = _flag_value(unit["ExecStart"][0], "--gateway-config")
    assert gateway == str(paths.config_file("gateway.toml"))


def test_the_deploy_script_asks_the_resolver_where_the_fleet_is() -> None:
    """The script must not carry its own answer — that is what disagreed with the unit.

    ``deploy-node.sh`` defaulted the fleet to one directory while the unit
    hardcoded another. Reading it from ``arc_team()`` makes a second answer
    impossible rather than merely wrong.
    """
    assert "arc_team" in _assignment("TEAM_ROOT"), (
        "TEAM_ROOT must come from arctrust.paths.arc_team(), not from a literal"
    )


def test_exporting_the_config_dir_cannot_move_the_fleet(monkeypatch, tmp_path) -> None:
    """Exporting the DEFAULT config dir must leave the fleet where it is.

    ``deploy-node.sh`` exports ARC_CONFIG_DIR for the runtime install. When
    ``arc_team()`` followed that value unconditionally, every ``arc`` call in the
    script answered ``~/.arc/team`` while the unit served ``~/arc/team`` — so
    ``arc agent create`` minted a second set of agents, with new DIDs and no
    personas, in a directory nothing serves. The box then started, loaded the
    real fleet, logged the full agent count and passed every health check, while
    message routing pointed at a DID absent from the served fleet. The same
    fallback later minted a second OPERATOR KEY under ``~/.arc/state``, signing
    module bundles with an issuer the deployment does not pin.

    The value carries no information when it names the default install home, so
    it no longer moves anything. A ``ARC_CONFIG_DIR`` pointing somewhere ELSE
    still means "isolated tree, keep everything inside it" — which is why the
    script must keep pinning ARC_TEAM_ROOT rather than relying on this.

    The basename guard could not see either bug: both sides spell the last
    component "team". Only the full path shows the parent is wrong.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / ".arc"))
    monkeypatch.delenv("ARC_TEAM_ROOT", raising=False)
    assert paths.arc_team() == tmp_path / "arc" / "team", (
        "naming the default install home must not relocate the fleet into it"
    )
    assert paths.arc_state() == tmp_path / "arc" / "state", (
        "nor the state root — that is where the duplicate operator key came from"
    )

    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "isolated"))
    assert paths.arc_team() == tmp_path / "isolated" / "team", (
        "an isolated tree must still take its fleet with it, or isolation is a lie"
    )

    assert "export ARC_TEAM_ROOT" in _script(), (
        "deploy-node.sh exports ARC_CONFIG_DIR; the fleet root must be pinned "
        "explicitly rather than inferred from it"
    )

    monkeypatch.setenv("ARC_TEAM_ROOT", str(tmp_path / "arc"))
    assert paths.arc_team() == tmp_path / "arc" / "team"


def test_the_runtime_stamp_does_not_come_from_the_target_s_git() -> None:
    """Naming the runtime from git reads the deploy target's stale ``.git``.

    The documented rsync excludes ``.git``, so ``git rev-parse`` on the box
    answers with whatever commit it was last cloned at. A DGX deploy installed
    536ff25e's code into ``runtime/0.2.0-1658fe71``. The name then collides on
    every later deploy, so ``current`` flips between two names that are one
    directory — and rollback silently does nothing.

    The stamp must describe the source, not the checkout it arrived from.
    """
    # BUILD_STAMP is a multi-line command substitution, so read the whole block
    # rather than the single line _assignment() returns.
    script = _script()
    start = script.index("BUILD_STAMP=")
    block = script[start : script.index("\nRUNTIME_VERSION=", start)]

    assert "rev-parse" not in block, (
        "BUILD_STAMP reads git on the deploy target, whose .git the rsync does not ship"
    )
    assert "shasum" in block, (
        "BUILD_STAMP must fingerprint the source tree so identical code reuses a "
        "directory and different code gets its own"
    )

    # scripts/ and deploy/ are consumed FROM the installed runtime — the unit is
    # copied out of runtime/current/deploy/systemd/, and install-nats.sh runs from
    # runtime/current/scripts/. Leaving them out of the fingerprint let a change
    # confined to either compute the same name and overwrite the ACTIVE runtime
    # in place. Caught on a live box after the first fix looked complete.
    for root in ("packages", "scripts", "deploy"):
        assert f'"$REPO_ROOT/{root}"' in block, (
            f"{root}/ is not fingerprinted, so a change confined to it reuses the "
            "running runtime's directory name"
        )
    for suffix in ("*.py", "*.toml", "*.sh", "*.service"):
        assert f"-name '{suffix}'" in block, f"{suffix} files are not fingerprinted"


def test_the_fleet_guard_compares_full_paths_not_basenames() -> None:
    """A guard that compares ``basename`` passes while the parent is wrong.

    ``~/.arc/team`` and ``~/arc/team`` share a basename, so the check meant to
    catch a relocated fleet matched both. The comparison must be the whole path.
    """
    script = _script()
    assert 'basename "$TEAM_ROOT"' not in script, (
        "the fleet-root guard compares basenames; ~/.arc/team and ~/arc/team both "
        "end in 'team', so it cannot see a fleet that moved"
    )
    assert '[ "$TEAM_ROOT" = "$EXPECTED_TEAM_ROOT" ]' in script, (
        "nothing in deploy-node.sh compares the resolved fleet root against the "
        "one this deploy targets"
    )


def test_the_deploy_script_installs_the_runtime_beside_its_siblings() -> None:
    """A versioned install directory is what makes the flip and the rollback exist."""
    assert "$RUNTIME_VERSION" in _assignment("RUNTIME_DIR")
    assert "runtime activate" in _script(), (
        "nothing in deploy-node.sh points `current` at the version it just installed"
    )


def test_the_deploy_script_never_runs_arc_from_the_checkout() -> None:
    """``$REPO_ROOT/.venv/bin/arc`` is the checkout's venv — the disposable-install bug."""
    arc_bin = _assignment("ARC_BIN")
    assert "REPO_ROOT" not in arc_bin
    assert "runtime" in arc_bin.lower()


# ---------------------------------------------------------------------------
# The fleet lives inside the rsync SOURCE, so the exclusion is load-bearing
# ---------------------------------------------------------------------------


def _rsync_invocation() -> str:
    """Return the rsync command deploy-node.sh uses to populate the runtime."""
    match = re.search(r"^rsync .*?(?=\n\S|\n\n)", _script(), re.MULTILINE | re.DOTALL)
    assert match, "deploy-node.sh no longer invokes rsync to install the runtime"
    return match.group(0)


def test_the_runtime_rsync_excludes_the_fleet(service_env: None) -> None:
    """The source tree CONTAINS the fleet. Without this, every deploy copies it.

    ``~/arc`` is both the rsync source and the fleet's parent, so a dropped
    exclude would rake four agents' memory, identity keys and workspaces into
    ``~/.arc/runtime/<version>/`` — a directory the next deploy deletes. The
    directory name is taken from ``arc_team()`` so renaming the fleet cannot
    silently leave this guarding the wrong thing.
    """
    fleet_dir = paths.arc_team().name
    excludes = re.findall(r"--exclude\s+'([^']+)'", _rsync_invocation())
    assert f"{fleet_dir}/" in excludes, (
        f"the rsync that installs the runtime must exclude '{fleet_dir}/' — "
        f"the fleet lives inside its source tree. Found: {excludes}"
    )


def test_the_deploy_script_verifies_the_exclusion_took_effect() -> None:
    """A flag can be edited away; an outcome check catches it however it happened.

    The exclusion is one word in a long command. Checking the RESULT — no fleet
    captured into the runtime, the real fleet still intact — catches a dropped
    flag, a renamed flag and a mis-anchored pattern alike, and aborts before the
    ``current`` symlink is flipped onto the bad tree.
    """
    script = _script()
    assert "$RUNTIME_DIR/$FLEET_DIR" in script, (
        "nothing verifies that the rsync did not capture the fleet into the runtime"
    )
    guard = script.index("$RUNTIME_DIR/$FLEET_DIR")
    activate = script.index('runtime activate "$RUNTIME_VERSION"')
    assert guard < activate, "the guard must abort BEFORE the runtime is activated"


def test_the_deploy_script_refuses_a_fleet_inside_the_delete_target() -> None:
    """``rsync --delete`` runs inside the runtime dir; a fleet under it would go.

    Only ``ARC_TEAM_ROOT`` can put one there, so the check is cheap — but it is
    the one clause that prevents deletion rather than reporting it afterwards,
    which is why it runs before rsync is invoked.
    """
    script = _script()
    assert "ARC_TEAM_ROOT" in script, "nothing checks where an overridden fleet root points"
    check = script.index("ARC_TEAM_ROOT", script.index("RUNTIME_DIR="))
    assert check < script.index("rsync -a"), "the containment check must run before rsync"
