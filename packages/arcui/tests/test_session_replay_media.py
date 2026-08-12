"""T-936 (REQ-318) — session replay names a media part; it never serves its bytes.

REQ-318: "WHEN the dashboard replays a session turn containing a media part THEN
it SHALL show the file's name and kind as a readable line rather than raw
structured data, and SHALL NOT serve the file's bytes to the browser."

Today a media part reaches the transcript as an unknown block shape and falls
through to the raw-JSON fallback in the SPA
(``web/src/components/llm-content-renderer.tsx`` ``ContentBlock`` ->
``<JsonBlock value={block} />``). The turn is present but unreadable. These
tests demand the readable line at the seam that can actually be gated: the
replay endpoint's payload. ``packages/arcui/web`` has no JS test runner, so a
rendering rule expressed only in TSX would ship unverified.

The security half is weighted deliberately. A media part is a *reference*: the
dashboard names it and stops there. Naming rather than displaying is what avoids
a route that streams workspace bytes to a browser with its own authorization,
size and cache behaviour.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from arcgateway import team_roster
from arcgateway.parts import MediaPart
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from arcui.audit import UIAuditLogger
from arcui.auth import AuthConfig, AuthMiddleware
from arcui.registry import AgentRegistry
from arcui.routes.agent_detail import routes as agent_detail_routes

_AGENT = "alpha"
_SID = "messaging:inbox"

# The bytes that must never leave the workspace. Distinctive enough that any
# leak — raw, base64 or escaped — is unambiguous in an assertion.
_IMAGE_BYTES = b"\x89PNG\r\n\x1a\nSECRET-PIXEL-PAYLOAD-DO-NOT-SERVE"

_MEDIA_PART = MediaPart(
    kind="image",
    mime="image/png",
    declared_name="q3-chart.png",
    ref="inbox/2026-08-11/153000-josh-q3-chart.png",
)


def _build_team_dir(tmp_path: Path) -> Path:
    """``team/alpha_agent/`` with a stored artefact and a turn referencing it."""
    root = tmp_path / "team"
    agent = root / "alpha_agent"
    agent.mkdir(parents=True)
    (agent / "arcagent.toml").write_text(
        '[agent]\nname = "alpha"\norg = "research"\ntype = "scout"\n'
        '[identity]\ndid = "did:arc:alpha"\n'
        '[secrets]\napi_key = "SHOULD_NEVER_LEAK"\n',
        encoding="utf-8",
    )
    workspace = agent / "workspace"

    # The artefact MediaStore wrote (COMP-002): real bytes on disk, referenced
    # from history but never carried in it.
    artefact = workspace / _MEDIA_PART.ref
    artefact.parent.mkdir(parents=True)
    artefact.write_bytes(_IMAGE_BYTES)

    sessions = workspace / "sessions"
    sessions.mkdir(parents=True)
    _write_session(sessions, _SID, _media_turns())
    return root


def _media_turns() -> list[dict[str, Any]]:
    """One text turn, then a turn whose content mixes text and a media part."""
    return [
        {
            "type": "message",
            "role": "user",
            "content": "here is the chart",
            "timestamp": "2026-08-11T15:30:00+00:00",
        },
        {
            # The shape the WIRED path actually writes. arcagent composes an
            # inbound turn through PartTranslator, which renders each artefact
            # as a readable line naming the file, its type and where it sits in
            # the workspace — the bytes never enter history (REQ-301/REQ-316).
            # Hand-rolling a raw MediaPart here would test a shape no producer
            # emits, and would have gone green against a pipeline that was
            # entirely unwired.
            "type": "message",
            "role": "user",
            "content": _composed_media_turn(),
            "timestamp": "2026-08-11T15:30:01+00:00",
        },
    ]



def _composed_media_turn() -> str:
    """Compose the media turn exactly as the agent does, via the real translator."""
    from arcagent.parts import PartTranslator

    blocks = PartTranslator(workspace=Path("/nonexistent")).to_history_content(
        [
            {"kind": "text", "text": "here is the chart"},
            _MEDIA_PART.model_dump(mode="json"),
        ]
    )
    return "\n".join(str(b.get("text", "")) for b in blocks).strip()


def _write_session(sessions: Path, sid: str, turns: list[dict[str, Any]]) -> None:
    (sessions / f"{sid}.jsonl").write_text(
        "".join(json.dumps(t) + "\n" for t in turns), encoding="utf-8"
    )


def _make_app(team_root: Path) -> tuple[Starlette, AuthConfig]:
    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app = Starlette(routes=list(agent_detail_routes))
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.agent_registry = AgentRegistry()
    app.state.audit = UIAuditLogger(enabled=False)
    app.state.team_root = team_root
    app.state.roster_provider = lambda: team_roster.list_team(
        team_root=team_root, online_ids=set()
    )
    return app, auth


def _viewer(auth: AuthConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth.viewer_token}"}


def _strings(value: Any) -> list[str]:
    """Every string value anywhere in a JSON payload.

    Deliberately shape-agnostic: the readable line may arrive as a rewritten
    content string, a new display field on the block, or a text block beside it.
    REQ-318 constrains what the operator reads, not where the server puts it.
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def _replay(client: TestClient, auth: AuthConfig, sid: str = _SID) -> dict[str, Any]:
    resp = client.get(f"/api/agents/{_AGENT}/sessions/{sid}", headers=_viewer(auth))
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


# --- 1. the readable line -------------------------------------------------


def test_media_turn_replays_as_a_readable_line_naming_file_and_kind(tmp_path: Path) -> None:
    """A media part reads as a line naming the file and its kind.

    RED until COMP-011 lands: today the part reaches the browser as the raw
    ``{kind, mime, declared_name, ref}`` object and renders through the SPA's
    unknown-block JSON fallback.
    """
    app, auth = _make_app(_build_team_dir(tmp_path))
    body = _replay(TestClient(app), auth)

    media_turn = body["messages"][1]
    readable = [
        s
        for s in _strings(media_turn)
        if _MEDIA_PART.declared_name in s and re.search(r"\bimage\b", s, re.IGNORECASE)
    ]
    assert readable, (
        "no readable line names both the file and its kind; the media part is "
        f"still raw structured data: {media_turn!r}"
    )

    # A line, not a JSON dump wearing a string's clothes.
    line = readable[0]
    assert '"declared_name"' not in line
    assert not line.lstrip().startswith(("{", "[")), f"readable line is raw JSON: {line!r}"


def test_text_only_turn_in_the_same_session_is_untouched(tmp_path: Path) -> None:
    """Adding the media line must not reshape ordinary text turns (REQ-315)."""
    app, auth = _make_app(_build_team_dir(tmp_path))
    body = _replay(TestClient(app), auth)

    assert body["total"] == 2
    assert body["messages"][0]["content"] == "here is the chart"
    assert body["messages"][0]["role"] == "user"


# --- 2. the bytes never reach the browser (REQ-318 security half) ---------


def test_replay_never_carries_the_artefact_bytes(tmp_path: Path) -> None:
    """The name travels; the payload does not — raw, base64 or escaped."""
    import base64

    app, auth = _make_app(_build_team_dir(tmp_path))
    client = TestClient(app)
    resp = client.get(f"/api/agents/{_AGENT}/sessions/{_SID}", headers=_viewer(auth))
    raw = resp.text

    assert _MEDIA_PART.declared_name in raw, "the file's NAME is the point of the line"
    assert "SECRET-PIXEL-PAYLOAD" not in raw
    assert base64.b64encode(_IMAGE_BYTES).decode("ascii") not in raw
    assert base64.b64encode(_IMAGE_BYTES).decode("ascii").rstrip("=") not in raw
    assert "\\u0089PNG" not in raw and "PNG" not in raw


def test_replay_response_stays_the_size_of_a_reference(tmp_path: Path) -> None:
    """A 5MB artefact must add bytes, not megabytes, to the replayed turn."""
    team = _build_team_dir(tmp_path)
    (team / "alpha_agent" / "workspace" / _MEDIA_PART.ref).write_bytes(b"\x00" * 5_000_000)
    app, auth = _make_app(team)
    client = TestClient(app)

    resp = client.get(f"/api/agents/{_AGENT}/sessions/{_SID}", headers=_viewer(auth))

    assert len(resp.content) < 8_192, "replay grew with the artefact — it is carrying bytes"


def test_no_route_serves_a_session_media_artefact(tmp_path: Path) -> None:
    """No endpoint exists to stream a replayed artefact's bytes.

    Guards the shape of the fix as much as the fix: REQ-318 is satisfied by
    naming the file, so COMP-011 must not arrive with an
    ``/api/agents/{id}/sessions/{sid}/media/...`` companion route that would
    need its own authorization, size ceiling and cache behaviour.
    """
    paths = [r.path for r in agent_detail_routes if isinstance(r, Route)]

    session_subroutes = [
        p for p in paths if "/sessions/" in p and not p.endswith("/sessions/{sid}")
    ]
    assert session_subroutes == [], f"new byte-serving replay route(s): {session_subroutes}"

    banned = re.compile(r"/(media|attachment|artefact|artifact|blob|raw|download)\b")
    assert [p for p in paths if banned.search(p)] == []


def test_a_media_ref_cannot_traverse_to_another_workspace_file(tmp_path: Path) -> None:
    """A ref is inert data on the replay path — history is read, refs are not.

    The ref is sender-adjacent by construction (an adapter hands up a payload),
    so a hostile one must not become a file read. Both the replay and the list
    route are checked: neither may dereference it.
    """
    team = _build_team_dir(tmp_path)
    sessions = team / "alpha_agent" / "workspace" / "sessions"
    hostile = MediaPart(
        kind="file",
        mime="application/toml",
        declared_name="notes.txt",
        ref="../../arcagent.toml",
    )
    _write_session(
        sessions,
        "hostile",
        [
            {
                "type": "message",
                "role": "user",
                "content": [hostile.model_dump(mode="json")],
                "timestamp": "2026-08-11T15:31:00+00:00",
            }
        ],
    )
    app, auth = _make_app(team)
    client = TestClient(app)

    replay = client.get(f"/api/agents/{_AGENT}/sessions/hostile", headers=_viewer(auth))
    listing = client.get(f"/api/agents/{_AGENT}/sessions", headers=_viewer(auth))

    assert replay.status_code == 200
    assert "SHOULD_NEVER_LEAK" not in replay.text
    assert "[secrets]" not in replay.text
    assert "SHOULD_NEVER_LEAK" not in listing.text
    # The listing enumerates session files only — a ref never becomes a row.
    assert all(r["path"].endswith(".jsonl") for r in listing.json()["sessions"])
