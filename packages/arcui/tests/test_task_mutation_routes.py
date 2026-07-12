"""``POST /api/team/tasks`` + ``PATCH /api/tasks/{id}`` — operator-gated mutation.

SPEC-056 Phase D (D4, FR-7). PLAN's Best Practices note: "copy
files_write.py:104-198 exactly — operator gate first (403 for viewer) ->
guard -> write -> audit via ``emit_mutation_audit``" and "409-on-in_progress:
after the operator gate, read the task's current status; if in_progress ->
``emit_mutation_audit(outcome='denied')`` + 409." This mirrors that shape,
plus ``team_chat.create_channel_route`` (team_chat.py:180-246) for the
create-resource wire convention (201, ``operator_role_required`` on the
role gate, raw resource dict in the body — no envelope).

Neither the ``arcui.routes.tasks`` module nor its routes exist yet — RED.
Contract this file assumes (for the GREEN implementer):

* New module ``arcui/routes/tasks.py`` exporting ``create_task``,
  ``patch_task``, and a ``routes`` list — wired into the app's route table
  alongside the other route modules (server.py).
* ``request.app.state.task_store`` is an ``arcstore.tasks.TaskStore`` opened
  against the SAME ``store/arcui.db`` as ``app.state.observe`` (mirrors
  ``arcagent.modules.tasks.store.open_store``).
* ``POST /api/team/tasks`` body: ``{"title": str, "description"?: str,
  "priority"?: str, "owner_did"?: str, "tags"?: list[str]}``. Success -> 201,
  body is the created task's fields (id/title/status/owner_did/creator_did/
  priority/...). Missing/blank ``title`` -> 400 ``{"error": ...}``.
* ``PATCH /api/tasks/{id}`` body: a partial patch dict (e.g. ``{"title":
  ...}``, ``{"priority": ...}``, ``{"owner_did": ...}``). Success -> 200,
  body is the updated task. Unknown id -> 404. ``status == "in_progress"``
  -> 409 ``{"error": "task_in_progress"}`` (edit-at-rest only, NFR-4).
* Both routes 403 ``{"error": "operator_role_required"}`` for a viewer token,
  before touching the store, and audit every outcome (applied/denied) via
  ``emit_mutation_audit`` with ``operation="task.create"``/``"task.update"``
  and ``target=f"task:{id}"``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from arcstore.backends.sqlite import SqliteBackend
from arcstore.tasks import Task, TaskStore
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.audit import UIAuditLogger
from arcui.auth import AuthConfig, AuthMiddleware

_CREATOR = "did:arc:test:human/operator"


async def _seed_store(data_dir: Path) -> TaskStore:
    backend = SqliteBackend(data_dir / "store" / "arcui.db")
    await backend.start()
    return TaskStore(backend)


def _task(id_: str, **overrides: Any) -> Task:
    fields: dict[str, Any] = {
        "id": id_,
        "title": f"Task {id_}",
        "creator_did": _CREATOR,
    }
    fields.update(overrides)
    return Task(**fields)


async def _seed(data_dir: Path, tasks: list[Task]) -> None:
    store = await _seed_store(data_dir)
    for t in tasks:
        await store.create(t)


def _make_app(tmp_path: Path) -> tuple[Starlette, AuthConfig]:
    from arcui.routes.tasks import routes as task_routes

    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app = Starlette(routes=task_routes)
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.audit = UIAuditLogger(enabled=False)
    app.state.task_store = asyncio.run(_seed_store(tmp_path))
    return app, auth


def _viewer(auth: AuthConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth.viewer_token}"}


def _operator(auth: AuthConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth.operator_token}"}


def _mutations(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    """Parse ``ui.mutation`` audit events emitted during the test."""
    out = []
    for record in caplog.records:
        if record.name != "arcui.audit":
            continue
        payload = json.loads(record.message)
        if payload["event_type"] == "ui.mutation":
            out.append(payload["details"])
    return out


class TestCreateTaskOperatorGate:
    def test_operator_creates_task_and_it_is_audited(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        app, auth = _make_app(tmp_path)
        app.state.audit = UIAuditLogger()  # real logger so caplog captures it
        client = TestClient(app)

        with caplog.at_level("INFO", logger="arcui.audit"):
            resp = client.post(
                "/api/team/tasks",
                headers=_operator(auth),
                json={"title": "Investigate the outage"},
            )

        assert resp.status_code == 201
        body = resp.json()
        assert body["title"] == "Investigate the outage"
        assert body["status"] == "backlog"  # unowned -> backlog (SDD §4)
        assert body["id"]

        mutations = _mutations(caplog)
        assert len(mutations) == 1
        assert mutations[0]["operation"] == "task.create"
        assert mutations[0]["outcome"] == "applied"
        assert mutations[0]["target"] == f"task:{body['id']}"

    def test_viewer_is_forbidden_and_denial_is_audited(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        app, auth = _make_app(tmp_path)
        app.state.audit = UIAuditLogger()
        client = TestClient(app)

        with caplog.at_level("INFO", logger="arcui.audit"):
            resp = client.post(
                "/api/team/tasks", headers=_viewer(auth), json={"title": "Nope"}
            )

        assert resp.status_code == 403
        assert resp.json() == {"error": "operator_role_required"}

        mutations = _mutations(caplog)
        assert len(mutations) == 1
        assert mutations[0]["operation"] == "task.create"
        assert mutations[0]["outcome"] == "denied"

        # Never reached the store — nothing was created.
        store = asyncio.run(_seed_store(tmp_path))
        assert asyncio.run(store.list()) == []

    def test_missing_title_is_400(self, tmp_path: Path) -> None:
        app, auth = _make_app(tmp_path)
        client = TestClient(app)

        resp = client.post("/api/team/tasks", headers=_operator(auth), json={})

        assert resp.status_code == 400


class TestPatchTaskAtRestOnly:
    def test_operator_edits_an_at_rest_task_and_it_is_audited(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        asyncio.run(_seed(tmp_path, [_task("t1", priority="low")]))
        app, auth = _make_app(tmp_path)
        app.state.audit = UIAuditLogger()
        client = TestClient(app)

        with caplog.at_level("INFO", logger="arcui.audit"):
            resp = client.patch(
                "/api/tasks/t1", headers=_operator(auth), json={"priority": "high"}
            )

        assert resp.status_code == 200
        assert resp.json()["priority"] == "high"

        mutations = _mutations(caplog)
        assert len(mutations) == 1
        assert mutations[0]["operation"] == "task.update"
        assert mutations[0]["outcome"] == "applied"
        assert mutations[0]["target"] == "task:t1"

    def test_viewer_is_forbidden(self, tmp_path: Path) -> None:
        asyncio.run(_seed(tmp_path, [_task("t1")]))
        app, auth = _make_app(tmp_path)
        client = TestClient(app)

        resp = client.patch("/api/tasks/t1", headers=_viewer(auth), json={"priority": "high"})

        assert resp.status_code == 403
        assert resp.json() == {"error": "operator_role_required"}

    def test_unknown_task_is_404(self, tmp_path: Path) -> None:
        app, auth = _make_app(tmp_path)
        client = TestClient(app)

        resp = client.patch(
            "/api/tasks/does-not-exist", headers=_operator(auth), json={"priority": "high"}
        )

        assert resp.status_code == 404

    def test_in_progress_task_is_409_and_denial_is_audited(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        asyncio.run(
            _seed(
                tmp_path,
                [_task("t1", owner_did="did:arc:x/aaaa", status="in_progress")],
            )
        )
        app, auth = _make_app(tmp_path)
        app.state.audit = UIAuditLogger()
        client = TestClient(app)

        with caplog.at_level("INFO", logger="arcui.audit"):
            resp = client.patch(
                "/api/tasks/t1", headers=_operator(auth), json={"priority": "critical"}
            )

        assert resp.status_code == 409
        assert resp.json() == {"error": "task_in_progress"}

        mutations = _mutations(caplog)
        assert len(mutations) == 1
        assert mutations[0]["operation"] == "task.update"
        assert mutations[0]["outcome"] == "denied"

        # Not mutated.
        store = asyncio.run(_seed_store(tmp_path))
        unchanged = asyncio.run(store.get("t1"))
        assert unchanged is not None and unchanged.priority == "medium"
