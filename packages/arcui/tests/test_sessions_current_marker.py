"""``GET /api/agents/{id}/sessions`` — current-session marker.

After a ``/new`` rotation the live chat writes to the rotation-aware
``current_session_key`` (folds the epoch generation into the key). The session
LIST must agree with that key: it exposes ``current_session_key`` for the caller
and flags the matching row ``current=True`` so a refresh/list surface resolves
the same session the chat writes to, not the stale generation-0 base key.
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


class _NullExecutor:
    async def run(self, event: Any) -> Any:  # pragma: no cover - never invoked
        raise AssertionError("listing must not run a turn")


def _write_session(sessions_dir: Path, key: str) -> None:
    (sessions_dir / f"{key}.jsonl").write_text(
        '{"type": "message", "role": "user", "content": "hi"}\n',
        encoding="utf-8",
    )


def _make_app(tmp_path: Path) -> tuple[Starlette, str, str, str]:
    """Build a sessions-list app rotated once; return (app, base, current, token)."""
    token = "viewer"
    user_did = derive_viewer_did(token)
    base_key = build_session_key(_AGENT_DID, user_did)

    router: SessionRouter = SessionRouter(executor=_NullExecutor())  # type: ignore[arg-type]
    router.new_session(_AGENT_DID, user_did)  # bump generation → gen 1
    current_key = router.current_session_key(_AGENT_DID, user_did)
    assert current_key != base_key  # rotation actually diverged the key

    agent_root = tmp_path / "alpha_agent"
    sessions_dir = agent_root / "workspace" / "sessions"
    sessions_dir.mkdir(parents=True)
    _write_session(sessions_dir, base_key)
    _write_session(sessions_dir, current_key)

    auth = AuthConfig({"viewer_token": token, "operator_token": "operator"})
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
            workspace_path=str(agent_root),
        )
    ]
    return app, base_key, current_key, token


def test_sessions_list_marks_current_after_rotation(tmp_path: Path) -> None:
    app, base_key, current_key, token = _make_app(tmp_path)
    client = TestClient(app)

    resp = client.get(
        f"/api/agents/{_AGENT_ID}/sessions",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # The list agrees with what the live chat writes to: the rotation-aware key.
    assert body["current_session_key"] == current_key
    assert body["current_session_key"] != base_key

    by_sid = {s["sid"]: s for s in body["sessions"]}
    assert by_sid[current_key]["current"] is True
    assert by_sid[base_key]["current"] is False
