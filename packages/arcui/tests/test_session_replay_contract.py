"""T-935 (REQ-316) — regression guard on the session list and replay contract.

**What this guards.** ``GET /api/agents/{id}/sessions`` and
``GET /api/agents/{id}/sessions/{sid}`` are the dashboard's only view of the
agent's own session history (``<workspace>/sessions/<key>.jsonl``, read through
``arcgateway.fs_reader`` per SPEC-022). SPEC-065 rewires the whole inbound
messaging path underneath that history and adds media parts to it. REQ-316 and
the operator constraint (US-8) say the history must stay listable and replayable
exactly as it is today.

**Why it exists.** This guard is expected to be GREEN before SPEC-065 starts and
to stay green through every task in it. If a later task changes the shape these
endpoints return for a plain text-only session, this file is what says so —
loudly, and at the seam the browser actually consumes, not at an internal
function that could be refactored around.

**How it is calibrated.** Assertions pin the keys and value types a caller
depends on, the pagination arithmetic, the newest-first ordering, and the fact
that a stored turn round-trips verbatim. They deliberately do NOT pin the exact
key *set*: REQ-315 permits additive change, so a new field must not fail this
guard, while a renamed, removed or retyped field must.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from arcgateway import team_roster
from arctrust.session_identity import build_session_key
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.audit import UIAuditLogger
from arcui.auth import AuthConfig, AuthMiddleware
from arcui.registry import AgentRegistry
from arcui.routes.agent_detail import routes as agent_detail_routes

_AGENT = "alpha"

# One text-only session, written in the exact shape SessionManager.append_message
# produces: type/role/content/timestamp, one JSON object per line.
_TEXT_TURNS: tuple[dict[str, object], ...] = (
    {"type": "message", "role": "user", "content": "hi", "timestamp": "2026-08-11T10:00:00+00:00"},
    {
        "type": "message",
        "role": "assistant",
        "content": "hello",
        "timestamp": "2026-08-11T10:00:01+00:00",
    },
    {"type": "message", "role": "user", "content": "go", "timestamp": "2026-08-11T10:00:02+00:00"},
)


def _build_team_dir(tmp_path: Path) -> Path:
    """Synthesise ``team/alpha_agent/`` with a text-only session history."""
    root = tmp_path / "team"
    agent = root / "alpha_agent"
    agent.mkdir(parents=True)
    (agent / "arcagent.toml").write_text(
        '[agent]\nname = "alpha"\norg = "research"\ntype = "scout"\n'
        '[identity]\ndid = "did:arc:alpha"\n',
        encoding="utf-8",
    )
    sessions = agent / "workspace" / "sessions"
    sessions.mkdir(parents=True)
    _write_session(sessions, "session-001", _TEXT_TURNS)
    return root


def _write_session(sessions: Path, sid: str, turns: tuple[dict[str, object], ...]) -> Path:
    path = sessions / f"{sid}.jsonl"
    path.write_text("".join(json.dumps(t) + "\n" for t in turns), encoding="utf-8")
    return path


def _make_app(team_root: Path) -> tuple[Starlette, AuthConfig]:
    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    registry = AgentRegistry()
    app = Starlette(routes=list(agent_detail_routes))
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.agent_registry = registry
    app.state.audit = UIAuditLogger(enabled=False)
    app.state.team_root = team_root
    app.state.roster_provider = lambda: team_roster.list_team(
        team_root=team_root, online_ids=set()
    )
    return app, auth


def _viewer(auth: AuthConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth.viewer_token}"}


# --- 1. the LIST endpoint -------------------------------------------------


def test_session_list_shape_is_unchanged(tmp_path: Path) -> None:
    """`GET /sessions` still returns `{sessions: [{sid, path, size, mtime}]}`."""
    team = _build_team_dir(tmp_path)
    app, auth = _make_app(team)
    client = TestClient(app)

    resp = client.get(f"/api/agents/{_AGENT}/sessions", headers=_viewer(auth))

    assert resp.status_code == 200
    body = resp.json()
    assert "sessions" in body, "top-level key `sessions` is the list contract"
    rows = body["sessions"]
    assert isinstance(rows, list)

    row = next(r for r in rows if r["sid"] == "session-001")
    assert {"sid", "path", "size", "mtime"} <= set(row), "a list row lost a field"
    assert isinstance(row["sid"], str)
    assert isinstance(row["path"], str)
    assert isinstance(row["size"], int)
    assert isinstance(row["mtime"], float)
    # `path` is the workspace-relative jsonl location, not an absolute path.
    assert row["path"].endswith("session-001.jsonl")
    assert not row["path"].startswith("/")


def test_session_list_is_newest_first(tmp_path: Path) -> None:
    """Ordering is part of the contract: the dashboard renders the list as given."""
    team = _build_team_dir(tmp_path)
    sessions = team / "alpha_agent" / "workspace" / "sessions"
    newer = _write_session(sessions, "session-002", _TEXT_TURNS)
    os.utime(newer, (2_000_000_000, 2_000_000_000))
    app, auth = _make_app(team)
    client = TestClient(app)

    rows = client.get(f"/api/agents/{_AGENT}/sessions", headers=_viewer(auth)).json()["sessions"]

    assert next(r["sid"] for r in rows) == "session-002"
    assert [r["mtime"] for r in rows] == sorted((r["mtime"] for r in rows), reverse=True)


_DM_TURNS: tuple[dict[str, object], ...] = (
    {
        "type": "message",
        "role": "user",
        "content": "Message from did:arc:beta (chat, normal priority):\n> ping",
        "timestamp": "2026-08-12T09:00:00+00:00",
    },
    {
        "type": "message",
        "role": "assistant",
        "content": "on it",
        "timestamp": "2026-08-12T09:00:05+00:00",
    },
    # A checkpoint line is loop metadata, not a turn — it must not be counted.
    {"type": "checkpoint", "turn_count": 1, "timestamp": "2026-08-12T09:00:06+00:00"},
)


def _add_teammate(team_root: Path, name: str, did: str) -> None:
    agent = team_root / f"{name}_agent"
    (agent / "workspace" / "sessions").mkdir(parents=True)
    (agent / "arcagent.toml").write_text(
        f'[agent]\nname = "{name}"\norg = "research"\ntype = "scout"\n[identity]\ndid = "{did}"\n',
        encoding="utf-8",
    )


def test_session_list_enriches_inbox_fields(tmp_path: Path) -> None:
    """Inbox needs kind/counterpart/message_count/last_role/last_text/last_ts.

    A teammate DM resolves to the peer's name via forward-hashing the roster; a
    bare human-chat key resolves to no peer and stays ``kind=chat``.
    """
    team = _build_team_dir(tmp_path)
    _add_teammate(team, "beta", "did:arc:beta")
    sessions = team / "alpha_agent" / "workspace" / "sessions"
    dm_sid = build_session_key("did:arc:alpha", "did:arc:beta")
    _write_session(sessions, dm_sid, _DM_TURNS)

    app, auth = _make_app(team)
    client = TestClient(app)
    rows = client.get(f"/api/agents/{_AGENT}/sessions", headers=_viewer(auth)).json()["sessions"]

    dm = next(r for r in rows if r["sid"] == dm_sid)
    assert dm["kind"] == "messaging"
    assert dm["counterpart"] == "beta"
    assert dm["message_count"] == 2  # checkpoint line excluded
    assert dm["last_role"] == "assistant"
    assert dm["last_text"] == "on it"
    assert dm["last_ts"] == "2026-08-12T09:00:05+00:00"

    chat = next(r for r in rows if r["sid"] == "session-001")
    assert chat["kind"] == "chat"
    assert chat["counterpart"] is None
    assert chat["message_count"] == 3
    assert chat["last_role"] == "user"
    assert chat["last_text"] == "go"


def test_session_list_unknown_agent_is_404(tmp_path: Path) -> None:
    team = _build_team_dir(tmp_path)
    app, auth = _make_app(team)
    client = TestClient(app)
    resp = client.get("/api/agents/ghost/sessions", headers=_viewer(auth))
    assert resp.status_code == 404
    assert "error" in resp.json()


# --- 2. the REPLAY endpoint ----------------------------------------------


def test_replay_shape_is_unchanged_for_text_only_session(tmp_path: Path) -> None:
    """`GET /sessions/{sid}` still returns `{sid, page, page_size, total, messages}`.

    And a stored turn round-trips verbatim: the endpoint hands the browser the
    parsed jsonl object, it does not reshape or re-key it.
    """
    team = _build_team_dir(tmp_path)
    app, auth = _make_app(team)
    client = TestClient(app)

    resp = client.get(f"/api/agents/{_AGENT}/sessions/session-001", headers=_viewer(auth))

    assert resp.status_code == 200
    body = resp.json()
    assert {"sid", "page", "page_size", "total", "messages"} <= set(body)
    assert body["sid"] == "session-001"
    assert isinstance(body["page"], int)
    assert isinstance(body["page_size"], int)
    assert body["total"] == len(_TEXT_TURNS)

    messages = body["messages"]
    assert isinstance(messages, list)
    assert all(isinstance(m, dict) for m in messages)
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "hi"
    assert messages[0]["type"] == "message"
    assert messages[0]["timestamp"] == "2026-08-11T10:00:00+00:00"
    assert [m["content"] for m in messages] == ["hi", "hello", "go"]


def test_replay_pagination_arithmetic_is_unchanged(tmp_path: Path) -> None:
    """`total` counts the whole history; `messages` is the requested slice."""
    team = _build_team_dir(tmp_path)
    app, auth = _make_app(team)
    client = TestClient(app)

    first = client.get(
        f"/api/agents/{_AGENT}/sessions/session-001?page=1&page_size=2", headers=_viewer(auth)
    ).json()
    second = client.get(
        f"/api/agents/{_AGENT}/sessions/session-001?page=2&page_size=2", headers=_viewer(auth)
    ).json()

    assert first["total"] == second["total"] == 3
    assert [m["content"] for m in first["messages"]] == ["hi", "hello"]
    assert [m["content"] for m in second["messages"]] == ["go"]
    assert first["page"] == 1 and first["page_size"] == 2


def test_replay_error_shapes_are_unchanged(tmp_path: Path) -> None:
    """Unknown session -> 404; traversal-shaped sid -> rejected; no token -> 401."""
    team = _build_team_dir(tmp_path)
    app, auth = _make_app(team)
    client = TestClient(app)

    missing = client.get(f"/api/agents/{_AGENT}/sessions/nope", headers=_viewer(auth))
    assert missing.status_code == 404
    assert "error" in missing.json()

    traversal = client.get(
        f"/api/agents/{_AGENT}/sessions/..%2Fetc%2Fpasswd", headers=_viewer(auth)
    )
    assert traversal.status_code in (400, 404)

    assert client.get(f"/api/agents/{_AGENT}/sessions/session-001").status_code == 401


def test_replay_tolerates_a_malformed_line(tmp_path: Path) -> None:
    """A corrupt line is skipped, not fatal — the transcript still replays."""
    team = _build_team_dir(tmp_path)
    sessions = team / "alpha_agent" / "workspace" / "sessions"
    with (sessions / "session-001.jsonl").open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")
    app, auth = _make_app(team)
    client = TestClient(app)

    body = client.get(f"/api/agents/{_AGENT}/sessions/session-001", headers=_viewer(auth)).json()

    assert body["total"] == len(_TEXT_TURNS)


def test_replay_tail_returns_the_newest_slice(tmp_path: Path) -> None:
    """``tail=1`` returns the LAST page_size turns — what a chat preload needs.

    Page 1 is the oldest slice, so a preload that requested it froze long
    conversations at their beginning: every message after row page_size looked
    lost while sitting safely in the jsonl.
    """
    team = _build_team_dir(tmp_path)
    app, auth = _make_app(team)
    client = TestClient(app)

    tail = client.get(
        f"/api/agents/{_AGENT}/sessions/session-001?page_size=2&tail=1", headers=_viewer(auth)
    ).json()

    assert tail["total"] == 3
    assert [m["content"] for m in tail["messages"]] == ["hello", "go"]
