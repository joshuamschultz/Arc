"""``arc install`` — the one command that takes a synced checkout to ready-to-start.

The failure this command exists against is not hypothetical. Modules live under
``${ARC_CONFIG_DIR:-~/.arc}/modules/`` and ship as separately signed bundles, so
a deployment that was updated with ``git pull && uv sync`` has *no* modules —
and every agent on it still boots, still answers chat, and still looks healthy
while its scheduler never fires. A live box was found in exactly that state.

So the contract asserted here is the operator's whole install→start story:

* a fresh deployment whose agents enable modules ends with those modules
  materialized where the agent's own discovery predicate finds them, and with an
  agent that really instantiates and registers their tools;
* running it a second time writes nothing and still exits 0 — an install that is
  not idempotent cannot be the thing a systemd unit or an upgrade runs;
* after it, ``arc up --check`` passes: the handoff has nothing left to do;
* a module a config asks for that cannot be delivered exits non-zero, because
  reporting success on a half-installed box is the original failure wearing a
  different hat;
* a missing ``nats-server`` does not block the install but does fail the
  command — the modules land, and the box is still not called ready.

Isolation is the same two env vars every suite here uses, plus
``ARC_MODULE_SOURCE``. Nothing may reach the developer's real ``~/.arc``: this
command materializes modules and rewrites agent configs, so a leak would write
real bytes into a real deployment.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import arcagent
import pytest
from arctrust.paths import ARC_CONFIG_DIR_ENV, bundles_dir, module_root, operator_dir

from arccli.commands import install as install_cmd
from arccli.commands import up as up_cmd

#: The real repository catalog — the same tree release CI packages from.
_SOURCE_CATALOG = Path(__file__).resolve().parents[2] / "arcagent" / "src" / "arcagent" / "modules"

#: Two modules that exist in the catalog and are absent from a fresh deployment.
#: Two rather than one so "installed everything the config asked for" is a real
#: claim rather than one that a single-module loop would satisfy by accident.
_MODULES = ("memory", "scheduler")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _agent_toml(agent_dir: Path, *, name: str, modules: tuple[str, ...]) -> str:
    body = (
        "[agent]\n"
        f"name = '{name}'\n"
        "org = 'testorg'\n"
        "type = 'executor'\n"
        f"workspace = '{agent_dir / 'workspace'}'\n\n"
        "[llm]\n"
        "model = 'test/model'\n\n"
        "[identity]\n"
        f"key_dir = '{agent_dir / 'keys'}'\n\n"
        "[telemetry]\n"
        "enabled = false\n\n"
        "[security]\ntier = 'personal'\n"
    )
    for module in modules:
        body += f"\n[modules.{module}]\nenabled = true\n"
    return body


def _make_agent(team_root: Path, name: str, modules: tuple[str, ...]) -> Path:
    agent_dir = team_root / name
    (agent_dir / "workspace").mkdir(parents=True)
    (agent_dir / "arcagent.toml").write_text(
        _agent_toml(agent_dir, name=name, modules=modules), encoding="utf-8"
    )
    return agent_dir


@pytest.fixture
def deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A fresh deployment: operator key, one agent enabling two modules, no modules on disk.

    Deliberately the exact post-``uv sync`` state of the production box — the
    config asks for capabilities that are nowhere on the filesystem.
    """
    root = tmp_path / "deployment"
    (root / "arc").mkdir(parents=True)
    (root / "store").mkdir()
    monkeypatch.setenv("ARC_CONFIG_DIR", str(root / "arc"))
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(root / "store"))
    monkeypatch.setenv("ARC_MODULE_SOURCE", str(_SOURCE_CATALOG))
    monkeypatch.setenv("ARCTEAM_NATS_URL", f"nats://127.0.0.1:{_free_port()}")
    monkeypatch.chdir(root)

    # A real file on PATH, so the preflight's own shutil.which is what answers.
    fake_bin = root / "bin"
    fake_bin.mkdir()
    nats = fake_bin / "nats-server"
    nats.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    nats.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    from arccli.commands.operator import ensure_operator_key

    ensure_operator_key(root / "arc")
    _make_agent(root / "team", "analyst", _MODULES)

    yield root


def _fingerprint(root: Path) -> str:
    """A digest of every byte under the deployment's module root and team dir.

    Idempotency is a claim about the filesystem, not about a printed line, so it
    is checked against content — a re-materialized module or a re-written
    ``[modules.NAME]`` entry changes this digest even when the output is
    identical.
    """
    digest = hashlib.sha256()
    for target in (module_root(root / "arc"), root / "team"):
        for path in sorted(p for p in target.rglob("*") if p.is_file()):
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# The whole point: a fresh deployment ends up whole
# ---------------------------------------------------------------------------


def test_a_fresh_deployment_gets_every_module_its_config_enables(
    deployment: Path, capsys: Any
) -> None:
    """One command, and the capabilities the config asked for are really on disk.

    Checked three ways because each alone can lie: the bytes exist at the
    deployment module root, ``arcagent``'s own discovery predicate — the thing
    that decides whether a module loads — finds them, and the per-agent
    capability copies landed too.
    """
    for module in _MODULES:
        assert module not in arcagent.discover_modules(), f"{module} was already installed"

    install_cmd.install_handler([])

    discovered = set(arcagent.discover_modules())
    for module in _MODULES:
        assert (module_root(deployment / "arc") / module).is_dir()
        assert module in discovered, f"the agent's own predicate does not find {module}"

    from arcbundle import capability_dir

    analyst = deployment / "team" / "analyst"
    for module in _MODULES:
        assert capability_dir(analyst, module).is_dir()

    out = capsys.readouterr().out
    assert "this deployment is ready" in out
    assert "Start it with: arc up" in out


async def test_the_installed_agent_boots_with_the_module_tools_registered(
    deployment: Path,
) -> None:
    """Materialized bytes are not the deliverable — a working capability is.

    A real agent is started from the very config ``arc install`` just rewrote,
    and its live tool registry is read while it is still up. This is the one
    thing a "modules installed" table cannot prove on its own: that the module
    reached the *runtime*, not merely the filesystem.

    The snapshot is taken before ``shutdown()``, which empties the registry — a
    set read afterwards is empty for an agent that was serving twenty tools a
    moment earlier, and every assertion here would then pass or fail for the
    wrong reason.
    """
    install_cmd.install_handler([])

    config_path = deployment / "team" / "analyst" / "arcagent.toml"
    model = MagicMock()
    model.close = AsyncMock()
    agent = arcagent.ArcAgent(config=arcagent.load_config(config_path), config_path=config_path)
    with (
        patch("arcagent.core.model_manager.load_eval_model", return_value=model),
        patch("arcagent.utils.model_helpers.load_eval_model", return_value=model),
    ):
        await agent.startup()
        assert agent._tool_registry is not None
        tools = set(agent._tool_registry.tools)
        bound = {binding.module_name for binding in agent._runtime_bindings}
        await agent.shutdown()

    assert set(_MODULES) <= bound, f"the installed modules never bound a runtime; bound {bound}"
    assert any(name.startswith("memory_") for name in tools), (
        f"the memory module registered no tool; the agent serves {sorted(tools)}"
    )


# ---------------------------------------------------------------------------
# Idempotency — the property an upgrade and a systemd unit depend on
# ---------------------------------------------------------------------------


def test_running_it_twice_changes_nothing_and_still_exits_zero(
    deployment: Path, capsys: Any
) -> None:
    """The second run must be a true no-op, byte for byte.

    An install that re-materializes on every invocation cannot be put in an
    ``ExecStartPre`` or an upgrade script: it would churn the module root under
    a running fleet and make every deploy log unreadable.
    """
    install_cmd.install_handler([])
    after_first = _fingerprint(deployment)
    capsys.readouterr()

    install_cmd.install_handler([])  # no SystemExit == exit code 0

    assert _fingerprint(deployment) == after_first, "the second install rewrote the deployment"
    out = capsys.readouterr().out
    assert "this deployment is ready" in out
    # Nothing was installed the second time — every module was already there.
    assert "installed" not in out.split("Verify")[0].split("Modules")[1]


# ---------------------------------------------------------------------------
# The handoff to `arc up`
# ---------------------------------------------------------------------------


def test_after_install_arc_up_check_passes_with_nothing_left_to_bootstrap(
    deployment: Path, capsys: Any
) -> None:
    """The install→start seam: ``arc up`` must find the box already whole.

    ``arc up --check`` skips the module stage entirely, so it can only pass on
    what ``arc install`` actually left behind. A green ``--check`` here is the
    proof that the two commands agree about what "installed" means.
    """
    install_cmd.install_handler([])
    capsys.readouterr()

    up_cmd.up_handler(["--check", "--port", str(_free_port()), "--no-browser"])

    out = capsys.readouterr().out
    assert "the deployment is whole" in out
    assert "MISSING" not in out


# ---------------------------------------------------------------------------
# Refusals — the command must not call a half-installed box ready
# ---------------------------------------------------------------------------


def test_a_module_that_cannot_be_installed_exits_non_zero(
    deployment: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """A config naming a capability no catalog can deliver is a failed install.

    The realistic cause is a typo in ``[modules.NAME]`` or a module dropped from
    the catalog. Either way the agent would run without it and say nothing, so
    the command reports the refusal, names the remedy, and exits non-zero rather
    than printing a table an operator would read as success.
    """
    _make_agent(deployment / "team", "typo_agent", ("schedulr",))
    monkeypatch.chdir(deployment)

    with pytest.raises(SystemExit) as exit_info:
        install_cmd.install_handler([])

    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    assert "REFUSED" in captured.out
    assert "MISSING" in captured.out
    assert "DEGRADED: typo_agent enables module 'schedulr'" in captured.err
    assert "still missing a capability its config asks for" in captured.err
    # The healthy agent was not punished for its sibling.
    assert set(_MODULES) <= set(arcagent.discover_modules())


def test_no_operator_key_stops_the_install_before_anything_is_written(
    deployment: Path, capsys: Any
) -> None:
    """The trust anchor is a precondition, never something an install mints.

    Without an operator key nothing can verify a bundle signature, so installing
    anyway would mean materializing unverified code. The command refuses, points
    at ``arc init``, and leaves the module root untouched.
    """
    for key in sorted(operator_dir(deployment / "arc").iterdir()):
        key.unlink()

    with pytest.raises(SystemExit) as exit_info:
        install_cmd.install_handler([])

    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    assert "operator key" in captured.out
    assert "arc init" in captured.out
    assert "nothing was installed" in captured.err
    assert not module_root(deployment / "arc").exists()


def test_a_missing_nats_server_installs_the_modules_and_still_fails_the_command(
    deployment: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """The two halves of "ready" are separable, and both are reported.

    Refusing to install modules because a broker binary is absent would leave the
    operator with neither — and modules are the half that rots silently. So the
    modules land. But the box cannot run without NATS, so ``arc install`` does
    not call it ready: exit non-zero, with the failing check named.

    Both lookup sources have to be emptied. The resolver deliberately searches
    the usual install directories after ``$PATH``, so clearing ``PATH`` alone
    leaves a developer's own ``/opt/homebrew/bin/nats-server`` answering and the
    test asserting nothing.
    """
    monkeypatch.setenv("PATH", str(deployment / "empty-bin"))
    monkeypatch.setattr("arcteam.nats_server._WELL_KNOWN_BIN_DIRS", ())

    with pytest.raises(SystemExit) as exit_info:
        install_cmd.install_handler([])

    assert exit_info.value.code == 1
    # The modules were installed anyway — that is the point of the split.
    assert set(_MODULES) <= set(arcagent.discover_modules())
    captured = capsys.readouterr()
    assert "nats-server" in captured.out
    assert "cannot run yet" in captured.err
    assert "nats-server" in captured.err


def test_no_team_root_refuses_rather_than_installing_nothing_quietly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """An empty deployment is an operator error, not a successful no-op.

    ``arc install`` returning 0 on a box with no agents would tell a deploy
    script the fleet is ready when there is no fleet at all.
    """
    empty = tmp_path / "empty"
    (empty / "arc").mkdir(parents=True)
    monkeypatch.setenv("ARC_CONFIG_DIR", str(empty / "arc"))
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(empty / "store"))
    monkeypatch.chdir(empty)

    with pytest.raises(SystemExit) as exit_info:
        install_cmd.install_handler([])

    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    assert "team root" in captured.out
    assert "nothing was installed" in captured.err


# ---------------------------------------------------------------------------
# One invocation, the whole fleet
# ---------------------------------------------------------------------------


def test_one_invocation_installs_for_every_agent_in_the_team_root(
    deployment: Path, capsys: Any
) -> None:
    """The fleet is the unit of work, not the agent.

    Doing this per agent meant looping ``--agent <name>`` by hand across six
    agents and seventeen modules — a hundred-odd invocations an operator has to
    get right, on a box where getting one wrong is silent. Three agents with
    overlapping and disjoint module sets prove the loop covers each agent's own
    set rather than installing one agent's list everywhere.
    """
    _make_agent(deployment / "team", "second", ("memory", "tasks"))
    _make_agent(deployment / "team", "third", ("workpad",))

    install_cmd.install_handler([])

    from arcbundle import capability_dir

    expected = {
        "analyst": set(_MODULES),
        "second": {"memory", "tasks"},
        "third": {"workpad"},
    }
    discovered = set(arcagent.discover_modules())
    for agent, modules in expected.items():
        assert modules <= discovered, f"{agent}'s modules are not materialized"
        for module in modules:
            assert capability_dir(deployment / "team" / agent, module).is_dir(), (
                f"{module} was not copied into {agent}"
            )
    assert "this deployment is ready" in capsys.readouterr().out


def test_an_agent_added_after_the_modules_were_materialized_still_gets_them(
    deployment: Path, capsys: Any
) -> None:
    """The bug that made a fleet report itself whole while two thirds of it was hollow.

    Materialization happens once at the shared deployment module root; the
    capability copy and the pinned issuer key are per agent. A predicate that
    asked only the shared question saw every module as "present" for an agent
    joining an already-installed fleet, skipped it entirely, and then verified
    green — an agent with no module tools at all, reported as healthy. Growing a
    fleet is the ordinary case, so this is not an edge.
    """
    from arcbundle import capability_dir

    install_cmd.install_handler([])
    assert set(_MODULES) <= set(arcagent.discover_modules()), "the fixture agent was not installed"

    _make_agent(deployment / "team", "latecomer", _MODULES)
    capsys.readouterr()

    install_cmd.install_handler([])  # no SystemExit == exit code 0

    latecomer = deployment / "team" / "latecomer"
    for module in _MODULES:
        assert capability_dir(latecomer, module).is_dir(), (
            f"{module} was never copied into the agent that joined later"
        )
    out = capsys.readouterr().out
    assert "installed" in out, "the latecomer's modules were skipped as already present"


def test_a_hollow_agent_is_reported_missing_rather_than_verified_green(
    deployment: Path, capsys: Any
) -> None:
    """The verdict itself: verify must refuse an agent whose copies are absent.

    Driven by deleting the capability copy while leaving the module materialized
    — the exact on-disk shape a late-joining agent had. Without the per-agent
    half of the predicate this exits 0 and prints ``present`` for every row.
    """
    from arcbundle import capability_dir

    install_cmd.install_handler([])
    shutil.rmtree(capability_dir(deployment / "team" / "analyst", "memory"))
    capsys.readouterr()

    states = up_cmd.agent_states(deployment / "team")

    assert states[0].missing == ("memory",), f"a hollow agent looked whole: {states[0]}"
    assert up_cmd.print_verify(states, []) is False


def test_bundles_are_built_automatically_when_none_are_staged(
    deployment: Path, capsys: Any
) -> None:
    """``arc module bundle`` is an implementation detail, never a step to discover.

    On a fresh box the bundle store is empty, so ``arc module install --all`` has
    literally nothing to install and says so without saying why. Nothing pointed
    at the missing first step. ``arc install`` stages what the fleet needs, signed
    with the deployment operator key so the result verifies at every tier.
    """
    store = bundles_dir(deployment / "arc")
    assert not store.exists(), "the deployment already had staged bundles"

    install_cmd.install_handler([])

    for module in _MODULES:
        bundle = store / f"{module}.arcbundle"
        assert (bundle / "manifest.json").is_file(), f"no signed bundle was staged for {module}"
    out = capsys.readouterr().out
    assert "built and signed by" in out
    assert "did:arc:operator" in out, "the bundle was not signed by the deployment operator"


def test_one_unbuildable_module_does_not_cost_the_others_their_bundles(
    deployment: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """A stale source directory holding only ``__pycache__`` took down a real deploy.

    That is what ``build_bundle`` reports as "contains no files to bundle", and
    aborting the batch on it left seventeen good modules unbundled. The bad one
    is reported and the rest are staged and installed; the command still exits
    non-zero, because a config asked for something the box does not have.
    """
    catalog = deployment / "catalog"
    shutil.copytree(_SOURCE_CATALOG, catalog, ignore=shutil.ignore_patterns("__pycache__"))
    hollow = catalog / "hollow"
    (hollow / "__pycache__").mkdir(parents=True)
    (hollow / "__pycache__" / "stale.pyc").write_bytes(b"\x00")
    monkeypatch.setenv("ARC_MODULE_SOURCE", str(catalog))
    _make_agent(deployment / "team", "unlucky", ("hollow", "workpad"))

    with pytest.raises(SystemExit) as exit_info:
        install_cmd.install_handler([])

    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    assert "REFUSED" in captured.out
    # Every other module the fleet asked for still landed.
    assert set(_MODULES) | {"workpad"} <= set(arcagent.discover_modules())
    assert "DEGRADED: unlucky enables module 'hollow'" in captured.err


# ---------------------------------------------------------------------------
# Tool discovery — the check must reach as far as the thing it checks
# ---------------------------------------------------------------------------


def test_nats_server_off_path_but_in_a_usual_directory_is_not_a_failure(
    deployment: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """A correctly provisioned box was refused a deploy over a binary it had.

    ``ssh host 'arc up --check'`` gets a login PATH without ``~/.local/bin``,
    which is precisely where the deploy script installs nats-server. The check
    said FAIL, and the deployment was blocked over nothing. A false FAIL that
    stops a deploy is worse than no check at all.

    The binary here is reachable ONLY through the well-known directory list —
    ``PATH`` is emptied — so a resolver that still only consulted ``PATH`` fails
    this test.
    """
    from arcteam.nats_server import find_nats_server

    local_bin = tmp_path / "local-bin"
    local_bin.mkdir()
    binary = local_bin / "nats-server"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path / "nothing-here"))
    monkeypatch.setattr("arcteam.nats_server._WELL_KNOWN_BIN_DIRS", (str(local_bin),))

    assert find_nats_server() == str(binary)

    install_cmd.install_handler([])  # no SystemExit == the check did not cry wolf

    out = capsys.readouterr().out
    assert "this deployment is ready" in out
    assert "FAIL" not in out


def test_the_resolver_still_prefers_path_and_still_reports_a_genuine_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Widening the search must not make the check unable to say "no".

    Two properties together: an operator who put a specific build on ``PATH``
    gets that one rather than a stale copy in a system directory, and a box with
    no binary anywhere still resolves to None so the FAIL is real.
    """
    from arcteam.nats_server import find_nats_server

    on_path, well_known = tmp_path / "path-bin", tmp_path / "well-known"
    for directory in (on_path, well_known):
        directory.mkdir()
        binary = directory / "nats-server"
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)
    monkeypatch.setattr("arcteam.nats_server._WELL_KNOWN_BIN_DIRS", (str(well_known),))

    monkeypatch.setenv("PATH", str(on_path))
    assert find_nats_server() == str(on_path / "nats-server"), "PATH must win"

    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr("arcteam.nats_server._WELL_KNOWN_BIN_DIRS", ())
    assert find_nats_server() is None, "a genuinely absent binary must still resolve to None"


# ---------------------------------------------------------------------------
# The newcomer's path: scaffold an agent, install, done
# ---------------------------------------------------------------------------


def test_a_freshly_scaffolded_agent_installs_clean(
    deployment: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """``arc agent create`` then ``arc install`` — the README sequence, exit 0.

    This is the shape a newcomer runs and the shape a defect hides in: the
    scaffold template is the *only* thing that decides which modules a fresh
    agent enables, so a block naming a module the catalog does not ship makes
    every new agent permanently un-startable — ``arc up`` refuses it, correctly,
    forever. It caught exactly that: a stale ``[modules.memory_acl]`` block left
    behind when that module was deleted.
    """
    from arccli.commands.agent import agent_handler

    agent_handler(["create", "newcomer", "--dir", "team", "--model", "test/model"])
    monkeypatch.chdir(deployment)
    capsys.readouterr()

    install_cmd.install_handler([])  # no SystemExit == exit code 0

    out = capsys.readouterr().out
    assert "this deployment is ready" in out
    assert "MISSING" not in out
    assert "REFUSED" not in out


def test_every_module_the_scaffold_enables_exists_in_the_catalog() -> None:
    """The invariant behind the test above, checked directly and cheaply.

    A ``[modules.NAME] enabled = true`` block whose ``NAME`` has no source in the
    catalog cannot ever be installed, so the config is asking for a capability
    that does not exist. Asserted against the real catalog rather than a list, so
    deleting a module without deleting its scaffold block fails here.
    """
    import tomllib

    from arccli.commands.agent._common import render_agent_config

    config = tomllib.loads(render_agent_config(name="probe", tier="personal"))
    enabled = {
        name
        for name, entry in config.get("modules", {}).items()
        if isinstance(entry, dict) and entry.get("enabled") is True
    }
    catalog = {path.name for path in _SOURCE_CATALOG.iterdir() if path.is_dir()}

    assert enabled, "the scaffold enables no modules at all — the template is broken"
    assert enabled <= catalog, (
        f"scaffold enables modules with no source: {sorted(enabled - catalog)}"
    )


# ---------------------------------------------------------------------------
# The verb itself
# ---------------------------------------------------------------------------


def test_install_is_registered_as_a_top_level_command() -> None:
    """A command a newcomer cannot find in ``arc --help`` is not "one command".

    Resolved through the registry's own lookup rather than by scanning the list,
    so this also proves ``install`` does not collide with ``arc module install``.
    """
    from arccli.commands.registry import resolve_command_and_args

    command, args = resolve_command_and_args(["install", "--team-root", "team"])

    assert command is not None
    assert command.name == "install"
    assert args == ["--team-root", "team"]

    module_command, module_args = resolve_command_and_args(["module", "install", "memory"])
    assert module_command is not None
    assert module_command.name == "module"
    assert module_args == ["install", "memory"]


def test_runtime_is_registered_as_a_top_level_command() -> None:
    """Rollback has to be findable in ``arc --help`` or it is not a rollback plan."""
    from arccli.commands.registry import resolve_command_and_args

    command, args = resolve_command_and_args(["runtime", "activate", "0.2.0"])

    assert command is not None
    assert command.name == "runtime"
    assert args == ["activate", "0.2.0"]


# ---------------------------------------------------------------------------
# --migrate-only — the layout move, without the rest of the install
# ---------------------------------------------------------------------------


def test_migrate_only_splits_the_home_and_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A deploy must split the home BEFORE any stage reads a config path.

    ``arc init`` creates what it does not find, so running it first writes a
    fresh config beside the real one — and the migration can then only refuse,
    because both are real. So the move needs a surface that runs on its own,
    ahead of every stage that resolves a path under the home.
    """
    home = tmp_path / "arc-home"
    monkeypatch.setenv(ARC_CONFIG_DIR_ENV, str(home))
    home.mkdir()
    (home / "gateway.toml").write_text("[gateway]\ntier = 'personal'\n", encoding="utf-8")

    install_cmd.install_handler(["--migrate-only"])

    from arctrust.paths import config_file

    assert (
        config_file("gateway.toml").read_text(encoding="utf-8") == "[gateway]\ntier = 'personal'\n"
    )
    assert not (home / "gateway.toml").exists()
    assert "Preflight" not in capsys.readouterr().out


def test_migrate_only_never_touches_the_fleet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fleet lives outside the home, so no migration can reach it.

    This is what makes the procedure cheap to run on a live box: an agent's
    memory, identity, tools, skills and workspace are never in the blast radius,
    whatever the home's shape turns out to be.
    """
    home = tmp_path / "arc-home"
    monkeypatch.setenv(ARC_CONFIG_DIR_ENV, str(home))
    monkeypatch.setenv("ARC_TEAM_ROOT", str(tmp_path / "arc"))
    home.mkdir()
    (home / "gateway.toml").write_text("[gateway]\n", encoding="utf-8")
    agent = tmp_path / "arc" / "team" / "josh_agent"
    (agent / "workspace").mkdir(parents=True)
    (agent / "arcagent.toml").write_text('[identity]\ndid = "did:arc:josh"\n', encoding="utf-8")

    install_cmd.install_handler(["--migrate-only"])

    from arctrust.paths import arc_team

    assert arc_team() == tmp_path / "arc" / "team"
    assert (agent / "arcagent.toml").read_text(encoding="utf-8") == (
        '[identity]\ndid = "did:arc:josh"\n'
    )
    assert (agent / "workspace").is_dir()


def test_migrate_only_is_re_runnable_on_a_box_that_needs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same procedure runs on two live boxes and is re-run after any failure."""
    monkeypatch.setenv(ARC_CONFIG_DIR_ENV, str(tmp_path / "arc-home"))

    install_cmd.install_handler(["--migrate-only"])
    install_cmd.install_handler(["--migrate-only"])
