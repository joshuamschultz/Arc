"""Items 72/73: the workflow view carries the owner's schedule row and tool safety.

A scheduled workflow that the safety breaker paused looked identical to a
healthy one on the dashboard, and a tool that duplicates its side effect on a
repeat looked identical to a safe one. Both facts already exist on disk (the
owner agent's ``schedules.json`` row; the extension manifest's ``idempotent``
flag); these tests pin that the plane reads them through the real files.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from arcstore.backends.memory import FakeBackend
from arcteam.workflow.runner import build_workflow_runner
from arctrust import OperatorKey

from arcui.routes.workflows import OperatorActor
from arcui.workflow_plane import (
    build_dashboard_plane,
    roster_schedule_reader,
    roster_tool_idempotency,
)

_DEFINITION = """
[workflow]
id = "nightly"
version = 1
owner = "@sales"

[[node]]
id = "send"
kind = "tool"
agent = "@sales"
tool = "send_mail"
"""

_MANIFEST = """
[[tools.declared]]
name = "send_mail"
classification = "external_effect"
idempotent = false

[[tools.declared]]
name = "list_mail"
classification = "read_only"
"""

_BREAKER_ROW = {
    "id": "wf:nightly",
    "type": "cron",
    "action": "workflow_run",
    "workflow_id": "nightly",
    "expression": "0 22 * * *",
    "enabled": False,
    "metadata": {
        "disabled_reason": "breaker",
        "disabled_at": "2026-10-01T22:00:05+00:00",
        "next_fire_at": "2026-10-02T22:00:00+00:00",
        "last_fired_at": "2026-10-01T22:00:00+00:00",
        "last_outcome": "error",
        "last_error": "start refused",
    },
}


def _roster(agent_root: Path) -> Any:
    entry = SimpleNamespace(agent_id="sales", workspace_path=str(agent_root))
    return lambda: [entry]


@pytest.fixture
def agent_root(tmp_path: Path) -> Path:
    root = tmp_path / "team" / "sales_agent"
    (root / "workspace").mkdir(parents=True)
    (root / "extensions" / "mail").mkdir(parents=True)
    (root / "extensions" / "mail" / "extension.toml").write_text(_MANIFEST, encoding="utf-8")
    return root


def _write_schedules(agent_root: Path, rows: list[dict[str, Any]]) -> None:
    (agent_root / "workspace" / "schedules.json").write_text(json.dumps(rows), encoding="utf-8")


@pytest.fixture
async def make_plane(tmp_path: Path) -> Any:
    OperatorKey.generate().save(tmp_path / "operator" / "operator.key")
    bundle = tmp_path / "workflows" / "nightly"
    bundle.mkdir(parents=True)
    (bundle / "workflow.toml").write_text(_DEFINITION, encoding="utf-8")
    backend = FakeBackend()
    await backend.start()
    runner = build_workflow_runner(
        tier="personal",
        task_store_backend=backend,
        runner_key_path=tmp_path / "operator" / "operator.key",
        workspace_root=tmp_path,
    )

    def _make(**wiring: Any) -> Any:
        return build_dashboard_plane(runner=runner, **wiring)

    yield _make
    await backend.stop()


_ACTOR = OperatorActor(did="did:arc:ui:operator", session_id="s1")


async def test_summary_includes_schedule_row_from_owner_store(
    make_plane: Any, agent_root: Path
) -> None:
    _write_schedules(agent_root, [_BREAKER_ROW])
    plane = make_plane(schedule_reader=roster_schedule_reader(_roster(agent_root)))

    detail = await plane.get_workflow("nightly", actor=_ACTOR)

    assert detail is not None
    assert detail["schedule"] == {
        "agent_id": "sales",
        "schedule_id": "wf:nightly",
        "enabled": False,
        "disabled_reason": "breaker",
        "disabled_at": "2026-10-01T22:00:05+00:00",
        "next_fire_at": "2026-10-02T22:00:00+00:00",
        "last_fired_at": "2026-10-01T22:00:00+00:00",
        "last_outcome": "error",
        "last_error": "start refused",
    }
    listed = await plane.list_workflows(actor=_ACTOR)
    assert listed[0]["schedule"]["disabled_reason"] == "breaker"


async def test_summary_schedule_is_none_without_a_row_or_reader(
    make_plane: Any, agent_root: Path
) -> None:
    _write_schedules(agent_root, [])
    with_reader = make_plane(schedule_reader=roster_schedule_reader(_roster(agent_root)))
    without_reader = make_plane()

    assert (await with_reader.get_workflow("nightly", actor=_ACTOR))["schedule"] is None
    assert (await without_reader.get_workflow("nightly", actor=_ACTOR))["schedule"] is None


def test_schedule_reader_survives_a_corrupt_schedules_file(agent_root: Path) -> None:
    (agent_root / "workspace" / "schedules.json").write_text("{not json", encoding="utf-8")

    assert roster_schedule_reader(_roster(agent_root))("@sales", "nightly") is None


def test_tool_idempotency_reads_the_extension_manifest(agent_root: Path) -> None:
    lookup = roster_tool_idempotency(_roster(agent_root))

    assert lookup("@sales", "send_mail") is False
    assert lookup("@sales", "list_mail") is True
    assert lookup("@sales", "unknown_tool") is None
    assert lookup("@nobody", "send_mail") is None


async def test_run_view_node_rows_carry_the_idempotent_flag(
    make_plane: Any, agent_root: Path
) -> None:
    plane = make_plane(tool_idempotent=roster_tool_idempotency(_roster(agent_root)))
    run_nodes: dict[str, dict[str, Any]] = {"send": {"node_id": "send", "status": "failed"}}

    plane._flag_unsafe_repeats("nightly", run_nodes)

    assert run_nodes["send"]["idempotent"] is False


async def test_workflow_node_rows_carry_the_idempotent_flag(
    make_plane: Any, agent_root: Path
) -> None:
    plane = make_plane(tool_idempotent=roster_tool_idempotency(_roster(agent_root)))

    detail = await plane.get_workflow("nightly", actor=_ACTOR)

    assert detail is not None
    assert detail["nodes"][0]["idempotent"] is False
