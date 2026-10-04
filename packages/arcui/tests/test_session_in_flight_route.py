"""``GET /api/agents/{id}/sessions/{sid}`` says whether a turn is running.

A person who leaves the chat and comes back mid-run needs the page to say the
agent is still working. The session router owns that fact (it counts turns in
flight per session key); the replay response carries it.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from arcgateway.identity import derive_viewer_did
from arcgateway.session import SessionRouter
from arctrust.session_identity import build_session_key
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.auth import AuthConfig, AuthMiddleware
from arcui.routes.agent_detail import routes as detail_routes

_AGENT_ID = "alpha"
_AGENT_DID = "did:arc:agent:alpha"
_TOKEN = "viewer"


class _NullExecutor:
    async def run(self, event: Any) -> Any:  # pragma: no cover - never invoked
        raise AssertionError("replay must not run a turn")


def _make_app(tmp_path: Path, router: SessionRouter) -> tuple[TestClient, str]:
    key = build_session_key(_AGENT_DID, derive_viewer_did(_TOKEN))
    root = tmp_path / "alpha_agent"
    sessions = root / "workspace" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / f"{key}.jsonl").write_text(
        '{"type": "message", "role": "user", "content": "still working on it?"}\n',
        encoding="utf-8",
    )
    auth = AuthConfig({"viewer_token": _TOKEN, "operator_token": "operator"})
    app = Starlette(routes=list(detail_routes))
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.session_router = router
    app.state.roster_provider = lambda: [
        SimpleNamespace(
            agent_id=_AGENT_ID,
            name=_AGENT_ID,
            did=_AGENT_DID,
            display_name="Alpha",
            workspace_path=str(root),
        )
    ]
    return TestClient(app), key


def _replay(client: TestClient, key: str) -> dict[str, Any]:
    resp = client.get(
        f"/api/agents/{_AGENT_ID}/sessions/{key}",
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )
    assert resp.status_code == 200
    body: dict[str, Any] = resp.json()
    return body


def test_replay_reports_idle_session_as_not_in_flight(tmp_path: Path) -> None:
    router: SessionRouter = SessionRouter(executor=_NullExecutor())  # type: ignore[arg-type]
    client, key = _make_app(tmp_path, router)

    body = _replay(client, key)

    assert body["run_in_flight"] is False
    assert body["messages"][0]["content"] == "still working on it?"


def test_replay_reports_running_turn_as_in_flight(tmp_path: Path) -> None:
    router: SessionRouter = SessionRouter(executor=_NullExecutor())  # type: ignore[arg-type]
    client, key = _make_app(tmp_path, router)
    router._in_flight[key] = 1  # the router's own bookkeeping while a turn runs

    assert _replay(client, key)["run_in_flight"] is True
