"""Why a run failed, over HTTP: the run list reason and the per-node timeline.

The nightly meeting ingest failed three times and the run list said only
"Failed". These drive the real routes over the real dashboard plane, runner and
stores; only the agent turn is replaced by writing the node rows directly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from arcstore.backends.memory import FakeBackend
from arcstore.tasks import SERVICE_RESTART_INTERRUPTED, TaskStore
from arcteam.workflow.runner import build_workflow_runner, node_task_id
from arctrust import OperatorKey
from starlette.applications import Starlette

from arcui.audit import UIAuditLogger
from arcui.auth import AuthConfig, AuthMiddleware
from arcui.routes.workflows import routes as workflow_routes
from arcui.workflow_plane import build_dashboard_plane

SALES = "did:arc:test:sales"

_DEFINITION = """
[workflow]
id = "ingest"
version = 1
owner = "@sales"

[[node]]
id = "collect"
kind = "agent"
agent = "@sales"

[[node]]
id = "filter_new"
kind = "agent"
agent = "@sales"
needs = ["collect"]
max_attempts = 3

[[node]]
id = "notify"
kind = "agent"
agent = "@sales"
needs = ["filter_new"]
deliver_to = "telegram:8293394811"
"""


class _Entity:
    did = SALES


class _Registry:
    async def get(self, handle: str) -> Any:
        return _Entity() if handle == "sales" else None


class _World:
    def __init__(self, tmp_path: Path, backend: FakeBackend) -> None:
        self.tasks = TaskStore(backend)
        self.runner = build_workflow_runner(
            tier="personal",
            task_store_backend=backend,
            runner_key_path=tmp_path / "operator" / "operator.key",
            workspace_root=tmp_path,
            registry=_Registry(),
        )
        auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
        app = Starlette(routes=workflow_routes)
        app.add_middleware(AuthMiddleware, auth_config=auth)
        app.state.auth_config = auth
        app.state.audit = UIAuditLogger(enabled=False)
        app.state.workflow_control_plane = build_dashboard_plane(runner=self.runner)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://arcui",
            headers={"Authorization": "Bearer operator"},
        )

    async def finish(self, run_id: str, node: str, iteration: int = 0, **fields: Any) -> None:
        row = node_task_id(run_id, node, iteration)
        await self.tasks.start_task(row, SALES)
        status = fields.pop("status", "done")
        await self.tasks.finish(row, status=status, resolution="ok", actor_did=SALES, **fields)
        await self.runner.advance(run_id)

    async def restart_kills(self, run_id: str, node: str, *, times: int) -> None:
        """The service dies under the node ``times`` times; each restart reclaims it."""
        row = node_task_id(run_id, node, 0)
        for _ in range(times):
            await self.tasks.start_task(row, SALES)
            current = await self.tasks.get(row)
            assert current is not None
            if current.attempts >= current.max_attempts:
                await self.tasks.dead_letter(
                    row,
                    actor_did=SALES,
                    resolution=f"failed after {current.attempts} attempt(s) — retries exhausted",
                    last_error=SERVICE_RESTART_INTERRUPTED,
                )
            else:
                await self.tasks.requeue(
                    row,
                    actor_did=SALES,
                    last_error=SERVICE_RESTART_INTERRUPTED,
                    next_attempt_at="2000-01-01T00:00:00+00:00",
                )
        await self.runner.advance(run_id)


@pytest.fixture
async def world(tmp_path: Path) -> AsyncIterator[_World]:
    OperatorKey.generate().save(tmp_path / "operator" / "operator.key")
    bundle = tmp_path / "workflows" / "ingest"
    bundle.mkdir(parents=True)
    (bundle / "workflow.toml").write_text(_DEFINITION, encoding="utf-8")
    backend = FakeBackend()
    await backend.start()
    built = _World(tmp_path, backend)
    yield built
    await built.client.aclose()
    await backend.stop()


async def _failed_run(world: _World) -> str:
    started = await world.client.post("/api/workflows/ingest/run", json={})
    assert started.status_code in (200, 201, 202), started.text
    run_id = str(started.json()["run_id"])
    await world.finish(run_id, "collect", output={"files": 3})
    await world.restart_kills(run_id, "filter_new", times=3)
    return run_id


async def test_run_list_says_which_node_failed_and_why(world: _World) -> None:
    run_id = await _failed_run(world)

    resp = await world.client.get("/api/workflows/ingest/runs")

    assert resp.status_code == 200
    (run,) = resp.json()["runs"]
    assert run["run_id"] == run_id and run["status"] == "failed"
    assert run["failure_reason"] == {
        "node_id": "filter_new",
        "summary": "The Arc service restarted while this step was running. Tried 3 times.",
        "detail": SERVICE_RESTART_INTERRUPTED,
    }


async def test_run_detail_gives_each_node_a_plain_reason_timing_and_delivery(
    world: _World,
) -> None:
    run_id = await _failed_run(world)

    resp = await world.client.get(f"/api/workflow-runs/{run_id}")

    assert resp.status_code == 200
    detail = resp.json()
    assert detail["failure_reason"]["node_id"] == "filter_new"
    nodes = {n["node_id"]: n for n in detail["nodes"]}
    failed = nodes["filter_new"]
    assert failed["status"] == "failed"
    assert failed["error_summary"] == "The Arc service restarted while this step was running."
    assert failed["last_error"] == SERVICE_RESTART_INTERRUPTED
    assert (failed["attempts"], failed["max_attempts"]) == (3, 3)
    assert failed["owner_did"] == SALES
    assert isinstance(nodes["collect"]["duration_s"], int | float)
    assert nodes["collect"]["duration_s"] >= 0
    assert nodes["notify"]["status"] == "cancelled"
    assert nodes["notify"]["deliver_to"] == "telegram:8293394811"
    assert nodes["notify"]["reason"].startswith("upstream filter_new failed")


async def test_a_node_that_recovered_shows_its_earlier_error_not_a_red_failure(
    world: _World,
) -> None:
    started = await world.client.post("/api/workflows/ingest/run", json={})
    run_id = str(started.json()["run_id"])
    await world.finish(run_id, "collect")
    await world.restart_kills(run_id, "filter_new", times=1)
    await world.finish(run_id, "filter_new", last_error=None)

    nodes = {
        n["node_id"]: n
        for n in (await world.client.get(f"/api/workflow-runs/{run_id}")).json()["nodes"]
    }

    recovered = nodes["filter_new"]
    assert recovered["status"] == "done"
    assert recovered["last_error"] is None
    assert recovered["recovered_from"] == {
        "summary": "The Arc service restarted while this step was running.",
        "detail": SERVICE_RESTART_INTERRUPTED,
    }


async def test_retry_from_the_failed_node_completes_the_run(world: _World) -> None:
    run_id = await _failed_run(world)

    retried = await world.client.post(f"/api/workflow-runs/{run_id}/nodes/filter_new/retry")
    assert retried.status_code == 200, retried.text
    await world.finish(run_id, "filter_new", iteration=1, output={"count": 1})
    await world.finish(run_id, "notify", iteration=1)

    detail = (await world.client.get(f"/api/workflow-runs/{run_id}")).json()
    assert detail["status"] == "done"
    assert detail["failure_reason"] is None
    listed = (await world.client.get("/api/workflows/ingest/runs")).json()["runs"][0]
    assert listed["failure_reason"] is None


async def test_a_secret_in_a_node_error_never_reaches_the_run_list_or_detail(
    world: _World,
) -> None:
    """Abuse: a provider error echoing an API key must not leak through the new fields."""
    secret = "sk-ant-api03-" + "A" * 40
    started = await world.client.post("/api/workflows/ingest/run", json={})
    run_id = str(started.json()["run_id"])
    await world.finish(run_id, "collect")
    row = node_task_id(run_id, "filter_new", 0)
    await world.tasks.start_task(row, SALES)
    await world.tasks.dead_letter(
        row, actor_did=SALES, resolution="failed", last_error=f"ArcLLMAPIError: bad key {secret}"
    )
    await world.runner.advance(run_id)

    listed = (await world.client.get("/api/workflows/ingest/runs")).text
    detail = (await world.client.get(f"/api/workflow-runs/{run_id}")).text

    assert secret not in listed and secret not in detail
    assert "filter_new" in listed, "the reason still names the node"
