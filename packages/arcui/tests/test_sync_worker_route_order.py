"""``GET /api/knowledge/sync-worker`` reaches the worker status, not an agent lookup.

``/api/knowledge/{agent_id}`` would otherwise swallow the literal path and answer
``agent 'sync-worker' not found``. The test goes through the real app's router.
"""

from __future__ import annotations

from pathlib import Path

from starlette.testclient import TestClient

from arcui.auth import AuthConfig
from arcui.server import create_app

OP_TOKEN = "o" * 64
VIEW_TOKEN = "v" * 64


def test_sync_worker_status_route_is_not_shadowed_by_agent_route(tmp_path: Path) -> None:
    app = create_app(
        auth_config=AuthConfig({"viewer_token": VIEW_TOKEN, "operator_token": OP_TOKEN}),
        team_root=tmp_path,
    )
    client = TestClient(app)

    response = client.get(
        "/api/knowledge/sync-worker", headers={"Authorization": f"Bearer {VIEW_TOKEN}"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "down"
