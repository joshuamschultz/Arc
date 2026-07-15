"""``/api/cancellations`` — operator-gated run kill switch (backend half).

GET lists pending (any role); POST parks a cancel request (operator-only). The
POST writes an attributable ``pending`` row a per-agent watcher later applies; a
viewer is refused and a request naming no target is a 400.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from arcstore.backends.sqlite import SqliteBackend
from arcstore.cancellations import CancelStore
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.audit import UIAuditLogger
from arcui.auth import AuthConfig, AuthMiddleware

_OPERATOR_DID = "did:arc:ui:operator"


async def _open_store(tmp_path: Path) -> CancelStore:
    backend = SqliteBackend(tmp_path / "store" / "arcui.db")
    await backend.start()
    return CancelStore(backend)


def _make_app(tmp_path: Path) -> tuple[Starlette, AuthConfig]:
    from arcui.routes.cancellations import routes as cancel_routes

    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app = Starlette(routes=cancel_routes)
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.audit = UIAuditLogger(enabled=False)
    app.state.cancel_store = asyncio.run(_open_store(tmp_path))
    return app, auth


def _viewer(auth: AuthConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth.viewer_token}"}


def _operator(auth: AuthConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth.operator_token}"}


def _pending(tmp_path: Path) -> list[Any]:
    async def _run() -> list[Any]:
        store = await _open_store(tmp_path)
        return await store.list(status="pending")

    return asyncio.run(_run())


def test_viewer_cannot_request_cancel(tmp_path: Path) -> None:
    app, auth = _make_app(tmp_path)
    client = TestClient(app)
    resp = client.post("/api/cancellations", headers=_viewer(auth), json={"run_id": "run-abc"})
    assert resp.status_code == 403
    assert _pending(tmp_path) == []


def test_operator_parks_attributable_request(tmp_path: Path) -> None:
    app, auth = _make_app(tmp_path)
    client = TestClient(app)
    resp = client.post(
        "/api/cancellations",
        headers=_operator(auth),
        json={"run_id": "run-abc", "reason": "too long"},
    )
    assert resp.status_code == 201, resp.text
    row = resp.json()
    assert row["run_id"] == "run-abc"
    assert row["reason"] == "too long"
    assert row["status"] == "pending"
    assert row["requested_by"] == _OPERATOR_DID

    pending = _pending(tmp_path)
    assert len(pending) == 1 and pending[0].run_id == "run-abc"


def test_missing_target_is_400(tmp_path: Path) -> None:
    app, auth = _make_app(tmp_path)
    client = TestClient(app)
    resp = client.post("/api/cancellations", headers=_operator(auth), json={"reason": "x"})
    assert resp.status_code == 400
    assert _pending(tmp_path) == []


def test_list_visible_to_viewer(tmp_path: Path) -> None:
    app, auth = _make_app(tmp_path)
    client = TestClient(app)
    client.post("/api/cancellations", headers=_operator(auth), json={"run_id": "run-abc"})

    resp = client.get("/api/cancellations", headers=_viewer(auth))
    assert resp.status_code == 200
    runs = [r["run_id"] for r in resp.json()["cancellations"]]
    assert runs == ["run-abc"]
