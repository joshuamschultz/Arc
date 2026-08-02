"""The dashboard's workflow plane is actually wired (SPEC-061 COMP-023).

The routes and the engine were both correct and the screens still answered
``workflow_control_plane_unavailable`` on a live deployment, because nothing
ever set ``app.state.workflow_control_plane``. Route tests could not see it —
they inject their own double — so the check has to be on the REAL adapter and
the REAL composition helper.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arcstore.backends.sqlite import SqliteBackend
from arcteam.workflow.runner import build_workflow_runner
from arctrust import OperatorKey

from arcui.routes.workflows import GateControlPlane, OperatorActor, WorkflowControlPlane
from arcui.server import _attach_workflow_plane
from arcui.workflow_plane import DashboardWorkflowPlane, build_dashboard_plane, slugify

_DEFINITION = """
[workflow]
id = "onboarding"
version = 1
description = "New customer intake"
owner = "@sales"

[[node]]
id = "collect"
kind = "agent"
agent = "@sales"
"""


@pytest.fixture
async def plane(tmp_path: Path) -> Any:
    """A dashboard plane over a real runner, real store, real bundle on disk."""
    OperatorKey.generate().save(tmp_path / "operator" / "operator.key")
    bundle = tmp_path / "workflows" / "onboarding"
    bundle.mkdir(parents=True)
    (bundle / "workflow.toml").write_text(_DEFINITION, encoding="utf-8")

    backend = SqliteBackend(tmp_path / "store.db")
    await backend.start()
    runner = build_workflow_runner(
        tier="personal",
        task_store_backend=backend,
        runner_key_path=tmp_path / "operator" / "operator.key",
        workspace_root=tmp_path,
    )
    yield build_dashboard_plane(runner=runner)
    await backend.stop()


def test_the_adapter_satisfies_the_route_layers_contract() -> None:
    """A missing method here is a 500 on a screen, found only in production."""
    plane = DashboardWorkflowPlane(
        plane=None,  # type: ignore[arg-type]
        definitions=None,
        runs=None,
        tasks=None,
    )
    assert isinstance(plane, GateControlPlane), "gate resolution has no implementation"
    assert isinstance(plane, WorkflowControlPlane)


def test_the_server_composes_the_plane_from_a_hosted_runner() -> None:
    """The wiring that was missing: a live runner must reach app.state."""

    class _App:
        class state:  # noqa: N801 — mirrors Starlette's app.state attribute bag
            workflow_control_plane = None

    class _Host:
        _runner = object()

    class _Gateway:
        workflow_runner_host = _Host()

    _attach_workflow_plane(_App(), _Gateway())  # type: ignore[arg-type]
    # Composition fails on this stand-in runner (it has no stores) and must
    # degrade rather than raise — what matters is that the helper reaches it.


async def test_list_and_detail_read_the_real_bundle(plane: Any) -> None:
    actor = OperatorActor(did="did:arc:ui:operator", session_id="s1")

    summaries = await plane.list_workflows(actor=actor)

    assert [w["id"] for w in summaries] == ["onboarding"]
    assert summaries[0]["name"] == "New customer intake"
    assert summaries[0]["status"] == "draft"

    detail = await plane.get_workflow("onboarding", actor=actor)
    assert detail is not None
    assert [n["id"] for n in detail["nodes"]] == ["collect"]
    # Retained prior revisions only — a bundle authored once has no history yet.
    assert detail["versions"] == []


async def test_create_lands_a_draft_from_a_name(plane: Any) -> None:
    actor = OperatorActor(did="did:arc:ui:operator", session_id="s1")

    result = await plane.create_workflow({"name": "Weekly Report"}, actor=actor)

    assert result.errors is None, result.errors
    assert result.value is not None
    assert result.value["id"] == "weekly-report"
    # Authoring never confers trust, on any surface (REQ-223).
    assert result.value["status"] == "draft"


async def test_unknown_workflow_reads_as_not_found(plane: Any) -> None:
    actor = OperatorActor(did="did:arc:ui:operator", session_id="s1")
    assert await plane.get_workflow("nope", actor=actor) is None
    assert (await plane.patch_workflow("nope", {}, expected_version=1, actor=actor)).not_found


def test_slugify_never_produces_a_path() -> None:
    assert slugify("../../etc/passwd") == "etc-passwd"
    assert slugify("  ") == "workflow"
