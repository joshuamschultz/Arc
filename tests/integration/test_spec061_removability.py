"""T-869 / SPEC-061 REQ-257 — every capability works with the dashboard gone.

The claim under test is architectural: arcui SHOWS actions that exist anyway, it
does not OWN any of them. A dashboard that quietly owned one operation would be
invisible while it is installed — every screen works, every test passes — and
would take that operation with it the moment a deployment ran headless, which is
the ordinary federal case.

So arcui is made genuinely unimportable for the whole module (a meta-path finder
that refuses it, not a monkeypatched attribute), and then the lifecycle is
driven three ways with no dashboard anywhere: the real ``arc workflow`` command
group, the real agent builder tools, and a real two-node run driven to
completion through the real runner over real arcstore rows.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.abc
import importlib.machinery
import json
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from arcstore.backends.memory import FakeBackend
from arcteam.agent_fleet import ArcTeamFleet
from arctrust.paths import workflows_dir

_ONE_NODE = """
[workflow]
id = "onboarding"
version = 1
owner = "@sales"

[[node]]
id = "collect"
kind = "agent"
agent = "@sales"
"""

_TWO_NODES = """
[workflow]
id = "onboarding"
version = 1
owner = "@sales"

[[node]]
id = "collect"
kind = "agent"
agent = "@sales"

[[node]]
id = "verify"
kind = "agent"
agent = "@ops"
needs = ["collect"]
"""

SALES_DID = "did:arc:local:agent/1111aaaa"
OPS_DID = "did:arc:local:agent/2222bbbb"


class _RefuseArcui(importlib.abc.MetaPathFinder):
    """An import hook that makes ``arcui`` behave as if it were never installed."""

    def find_spec(
        self, fullname: str, path: Sequence[str] | None = None, target: Any = None
    ) -> importlib.machinery.ModuleSpec | None:
        if fullname == "arcui" or fullname.startswith("arcui."):
            raise ModuleNotFoundError(f"No module named {fullname!r} (uninstalled for this test)")
        return None


@pytest.fixture(autouse=True)
def _no_dashboard() -> Iterator[None]:
    """Uninstall arcui for the duration of every test in this module."""
    evicted = {name: mod for name, mod in sys.modules.items() if name.split(".")[0] == "arcui"}
    for name in evicted:
        del sys.modules[name]
    finder = _RefuseArcui()
    sys.meta_path.insert(0, finder)
    try:
        yield
    finally:
        sys.meta_path.remove(finder)
        sys.modules.update(evicted)


@pytest.fixture(autouse=True)
def _isolated_arc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Roots and substrate resolve from the environment, as in a real deployment.

    An empty team-bus url is the honest solo-box configuration: the bindings are
    built over the in-process bus rather than reaching for a NATS server that a
    test machine has no reason to be running.
    """
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc"))
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ARCTEAM_NATS_URL", "")


@pytest.fixture
def arcstore_backend(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeBackend]:
    """Bind every CLI/control-plane call in a test to one explicit backend.

    Production ArcStore is PostgreSQL-only on this branch. These tests exercise
    the workflow surfaces, so a contract fake keeps the test focused while the
    same instance lets CLI writes and read-backs observe one durable store.
    """
    from arccli.commands import workflow as workflow_command

    backend = FakeBackend()
    monkeypatch.setattr(workflow_command, "_backend_factory", lambda: backend)
    yield backend


@pytest.fixture
def arc_dir(tmp_path: Path) -> Path:
    """The Arc home with a bootstrapped operator key, as first run leaves it."""
    from arccli.commands.operator import load_operator_key

    home = tmp_path / "arc"
    home.mkdir(parents=True, exist_ok=True)
    load_operator_key(home)
    return home


def _write_bundle(root: Path, document: str = _TWO_NODES, wid: str = "onboarding") -> Path:
    bundle = root / wid
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "workflow.toml").write_text(document, encoding="utf-8")
    return bundle


def test_the_dashboard_really_is_uninstalled() -> None:
    """Guard the guard: without this, every test below could pass vacuously."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("arcui")


def test_the_whole_operator_lifecycle_runs_from_the_command_line(
    arc_dir: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    arcstore_backend: FakeBackend,
) -> None:
    """create, view, edit, archive, unarchive, sign, run, cancel, purge — headless."""
    from arccli.commands.workflow import workflow_handler

    source = _write_bundle(tmp_path / "src", _ONE_NODE)

    workflow_handler(["create", str(source), "--dir", str(arc_dir)])
    assert "Created draft workflow onboarding" in capsys.readouterr().out

    workflow_handler(["list", "--dir", str(arc_dir)])
    assert "onboarding" in capsys.readouterr().out

    workflow_handler(["show", "onboarding", "--dir", str(arc_dir)])
    assert '"id": "onboarding"' in capsys.readouterr().out

    (source / "workflow.toml").write_text(_TWO_NODES, encoding="utf-8")
    workflow_handler(
        [
            "edit",
            "onboarding",
            "--document",
            str(source),
            "--expected-version",
            "1",
            "--reason",
            "add the verify node",
            "--dir",
            str(arc_dir),
        ]
    )
    assert "-> v2" in capsys.readouterr().out

    workflow_handler(["archive", "onboarding", "--dir", str(arc_dir)])
    assert "Archived onboarding." in capsys.readouterr().out
    workflow_handler(["unarchive", "onboarding", "--dir", str(arc_dir)])
    assert "Unarchived onboarding" in capsys.readouterr().out

    bundle = workflows_dir(arc_dir) / "onboarding"
    workflow_handler(["sign", str(bundle), "--dir", str(arc_dir)])
    assert "Signed workflow.toml" in capsys.readouterr().out

    workflow_handler(["run", "onboarding", "--detach", "--dir", str(arc_dir)])
    started = capsys.readouterr().out
    assert "Started run" in started
    run_id = started.split("Started run ")[1].split(" ")[0]
    assert _run_row(run_id, arcstore_backend)["workflow_id"] == "onboarding"

    workflow_handler(["cancel", run_id, "--dir", str(arc_dir)])
    assert "Cancelled" in capsys.readouterr().out

    workflow_handler(["purge", "onboarding", "--force", "--dir", str(arc_dir)])
    assert "Purged onboarding." in capsys.readouterr().out
    assert not bundle.exists()


def _run_row(run_id: str, backend: FakeBackend) -> dict[str, Any]:
    """Read the durable Run the CLI just wrote — proof it reached the real engine."""
    from arcstore.runs import RunStore

    async def _read() -> dict[str, Any]:
        await backend.start()
        try:
            run = await RunStore(backend).get(run_id)
            assert run is not None, f"the CLI reported run {run_id} but wrote no Run row"
            return {"workflow_id": run.workflow_id, "status": run.status}
        finally:
            await backend.stop()

    return asyncio.run(_read())


def test_the_command_line_runner_can_resolve_node_owners(
    arc_dir: Path, arcstore_backend: FakeBackend
) -> None:
    """A CLI-started run must reach a runner wired to the team registry.

    Without this binding ``arc workflow run`` prints "Started run …" and the run
    is already dead: no ``@handle`` resolves, so the first node fails with
    "unknown agent". A refusal that reads as a start is the exact silent shape
    the dashboard's absence would otherwise be blamed for.
    """
    from arccli.commands.workflow import _resolve_control_plane
    from arcteam.workflow.runner import _NoRegistry
    from arcteam.workflow.stores import RegistryOwnerResolver

    async def _owners() -> Any:
        plane, aclose = await _resolve_control_plane(arc_dir, tier="personal")
        try:
            return plane._runner._owners
        finally:
            await aclose()

    resolver = asyncio.run(_owners())
    assert not isinstance(resolver, _NoRegistry)
    assert isinstance(resolver, RegistryOwnerResolver)


def test_the_authoring_lifecycle_runs_from_the_agent_tools(
    arc_dir: Path, arcstore_backend: FakeBackend
) -> None:
    """An agent authors, extends, lists, and inspects a workflow with no dashboard."""
    from arcagent.modules.workflows import _runtime
    from arcagent.modules.workflows.capabilities import (
        workflow_add_node,
        workflow_create,
        workflow_inspect,
        workflow_list,
    )
    from arccli.commands.operator import operator_key_path
    from arctrust import AgentIdentity, OperatorKey

    async def _drive() -> None:
        _runtime.reset()
        _runtime.configure(
            config={},
            workspace=arc_dir,
            identity=AgentIdentity.generate(org="local", agent_type="agent"),
            operator_signer=OperatorKey.load(
                operator_key_path(arc_dir), generate_if_absent=False
            ).into_signer(),
            fleet=ArcTeamFleet(),
            arcstore_opener=lambda: _open_backend(arcstore_backend),
        )
        try:
            created = json.loads(
                await workflow_create(
                    workflow_id="agent-authored",
                    description="authored with no dashboard",
                    owner="@sales",
                    nodes=[{"id": "draft", "kind": "agent", "agent": "@sales"}],
                )
            )
            assert "errors" not in created, created
            assert created["status"] == "draft"

            added = json.loads(
                await workflow_add_node(
                    workflow_id="agent-authored",
                    expected_version=created["definition"]["version"],
                    node={"id": "review", "kind": "agent", "agent": "@ops", "needs": ["draft"]},
                )
            )
            assert "errors" not in added, added

            listed = json.loads(await workflow_list())
            assert any(w["definition"]["id"] == "agent-authored" for w in listed), listed

            inspected = json.loads(await workflow_inspect(workflow_id="agent-authored"))
            node_ids = [n["id"] for n in inspected["definition"]["nodes"]]
            assert node_ids == ["draft", "review"]
        finally:
            _runtime.reset()

    asyncio.run(_drive())


async def _open_backend(backend: FakeBackend) -> FakeBackend:
    """Return the shared contract fake through the async runtime seam."""
    return backend


def test_a_run_completes_end_to_end_with_no_dashboard(
    arc_dir: Path, arcstore_backend: FakeBackend
) -> None:
    """The execution half: a real runner drives a real two-node run to done.

    The dashboard renders runs; nothing about progressing one may depend on it.
    """
    from arccli.commands.operator import operator_key_path
    from arcstore.tasks import TaskStore
    from arcteam.workflow.runner import build_workflow_runner

    _write_bundle(arc_dir / "workflows")

    class Registry:
        """The one method the runner asks of the entity registry."""

        def __init__(self, mapping: dict[str, str]) -> None:
            self._mapping = mapping

        async def get(self, ref: str) -> Any:
            did = self._mapping.get(ref)
            return None if did is None else type("Entity", (), {"did": did})()

    async def _drive() -> str:
        backend = arcstore_backend
        await backend.start()
        try:
            runner = build_workflow_runner(
                tier="personal",
                task_store_backend=backend,
                runner_key_path=operator_key_path(arc_dir),
                workspace_root=arc_dir,
                registry=Registry({"sales": SALES_DID, "ops": OPS_DID}),
            )
            tasks = TaskStore(backend)
            run = await runner.start_run(
                "onboarding", input={}, initiator_did="did:arc:local:operator/test"
            )
            for node_id, did in (("collect", SALES_DID), ("verify", OPS_DID)):
                row = f"wf/{run.run_id}/{node_id}/0"
                await tasks.start_task(row, did)
                await tasks.finish(
                    row, status="done", resolution="ok", actor_did=did, output={"ok": True}
                )
                run = await runner.advance(run.run_id)
            await runner.aclose()
            return str(run.status)
        finally:
            await backend.stop()

    assert asyncio.run(_drive()) == "done"
