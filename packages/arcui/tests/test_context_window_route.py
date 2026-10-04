"""``GET /api/agents/{id}/context-window`` — the last real chat prompt.

The agent-detail Context Window card used to read the newest model call of ANY
kind, so a 312-token background call hid a nearly full chat. The route must pick
the newest completed main-model chat prompt of the agent's chat session, and
nothing else.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from arcgateway.identity import derive_viewer_did
from arcgateway.session import SessionRouter
from arcstore.records import SpoolRecord
from arcstore.spool import record as spool_record
from arctrust.session_identity import build_session_key
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.auth import AuthConfig, AuthMiddleware
from arcui.observe import Observe
from arcui.routes.agent_detail import routes as detail_routes

_AGENT_ID = "mc"
_AGENT_DID = "did:arc:agent:mc"
_TOKEN = "viewer"
_OTHER_SESSION = "other-session-key"


class _NullExecutor:
    async def run(self, event: Any) -> Any:  # pragma: no cover - never invoked
        raise AssertionError("reading must not run a turn")


def _call(
    data_dir: Path,
    *,
    ts: str,
    prompt_tokens: int,
    session_id: str | None,
    agent_label: str = "mc",
    call_origin: str | None = "chat",
    operation: str | None = None,
    outcome: str = "ok",
    cache_read: int | None = None,
    cache_write: int | None = None,
) -> None:
    extra: dict[str, Any] = {}
    if session_id is not None:
        extra["session_id"] = session_id
    if call_origin is not None and session_id is not None:
        extra["call_origin"] = call_origin
    if operation is not None:
        extra["operation"] = operation
    spool = data_dir / "spool"
    spool.mkdir(parents=True, exist_ok=True)
    spool_record(
        SpoolRecord(
            kind="llm_call",
            actor_did=_AGENT_DID,
            agent_label=agent_label,
            request_id=f"run-{ts}",
            model="claude-test",
            prompt_tokens=prompt_tokens,
            completion_tokens=40,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            outcome=outcome,
            ts=ts,
            extra=extra,
        ),
        path=spool / "operational-2026-10-04.jsonl",
    )


def _client(tmp_path: Path, observe: Observe, router: SessionRouter) -> tuple[TestClient, str]:
    chat_key = build_session_key(_AGENT_DID, derive_viewer_did(_TOKEN))
    root = tmp_path / "mc_agent"
    sessions = root / "workspace" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / f"{chat_key}.jsonl").write_text(
        '{"type": "message", "role": "user", "content": "hi"}\n', encoding="utf-8"
    )
    auth = AuthConfig({"viewer_token": _TOKEN, "operator_token": "operator"})
    app = Starlette(routes=list(detail_routes))
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.observe = observe
    app.state.session_router = router
    app.state.roster_provider = lambda: [
        SimpleNamespace(
            agent_id=_AGENT_ID,
            name=_AGENT_ID,
            did=_AGENT_DID,
            display_name="MC",
            workspace_path=str(root),
        )
    ]
    return TestClient(app), chat_key


def _get(client: TestClient) -> dict[str, Any]:
    resp = client.get(
        f"/api/agents/{_AGENT_ID}/context-window", headers={"Authorization": f"Bearer {_TOKEN}"}
    )
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


@pytest.fixture()
def router() -> SessionRouter:
    return SessionRouter(executor=_NullExecutor())  # type: ignore[arg-type]


async def test_main_chat_prompt_wins_over_newer_noise(
    tmp_path: Path, router: SessionRouter
) -> None:
    chat_key = build_session_key(_AGENT_DID, derive_viewer_did(_TOKEN))
    data = tmp_path / "data"
    # The real chat prompt, oldest of the mix.
    _call(
        data,
        ts="2026-10-04T10:00:00+00:00",
        prompt_tokens=162_000,
        session_id=chat_key,
        cache_read=150_000,
        cache_write=11_000,
    )
    # Newer noise, each of which the old card would have shown.
    _call(  # embedding
        data,
        ts="2026-10-04T10:01:00+00:00",
        prompt_tokens=20,
        session_id=None,
        operation="embed:x",
    )
    _call(  # background memory distill
        data,
        ts="2026-10-04T10:02:00+00:00",
        prompt_tokens=312,
        session_id=None,
        agent_label="mc/distill",
    )
    _call(  # same session id but a background origin
        data,
        ts="2026-10-04T10:03:00+00:00",
        prompt_tokens=400,
        session_id=chat_key,
        call_origin="background",
    )
    _call(  # another session's chat prompt
        data, ts="2026-10-04T10:04:00+00:00", prompt_tokens=900, session_id=_OTHER_SESSION
    )
    _call(  # the chat session's own failed call
        data,
        ts="2026-10-04T10:05:00+00:00",
        prompt_tokens=0,
        session_id=chat_key,
        outcome="error",
    )
    observe = Observe(data_dir=data)
    await observe.refresh()
    client, _ = _client(tmp_path, observe, router)

    body = _get(client)

    assert body["session_id"] == chat_key
    assert body["turn_in_flight"] is False
    prompt = body["prompt"]
    assert prompt["input_tokens"] == 162_000
    assert prompt["cache_read_tokens"] == 150_000
    assert prompt["cache_write_tokens"] == 11_000
    assert prompt["timestamp"].startswith("2026-10-04T10:00:00")


async def test_no_chat_prompt_yet_reports_none(tmp_path: Path, router: SessionRouter) -> None:
    data = tmp_path / "data"
    _call(data, ts="2026-10-04T10:00:00+00:00", prompt_tokens=312, session_id=None)
    observe = Observe(data_dir=data)
    await observe.refresh()
    client, _ = _client(tmp_path, observe, router)

    assert _get(client)["prompt"] is None


async def test_running_turn_is_flagged_and_last_completed_prompt_still_shown(
    tmp_path: Path, router: SessionRouter
) -> None:
    chat_key = build_session_key(_AGENT_DID, derive_viewer_did(_TOKEN))
    data = tmp_path / "data"
    _call(data, ts="2026-10-04T10:00:00+00:00", prompt_tokens=150_000, session_id=chat_key)
    observe = Observe(data_dir=data)
    await observe.refresh()
    client, _ = _client(tmp_path, observe, router)
    router._in_flight[chat_key] = 1

    body = _get(client)

    assert body["turn_in_flight"] is True
    assert body["prompt"]["input_tokens"] == 150_000
