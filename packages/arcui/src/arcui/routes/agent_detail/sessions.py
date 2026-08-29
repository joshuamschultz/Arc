"""`/api/agents/{id}/sessions[/{sid}]` + tasks/schedules route handlers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from arcgateway import fs_reader
from arcgateway.fs_reader import FileTooLargeError, PathTraversalError
from arcgateway.identity import derive_viewer_did
from arctrust.session_identity import build_session_key
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse

from arcui.query_validators import parse_pagination
from arcui.routes.agent_detail._common import _CALLER_DID, _VALID_SID, _agent_did, _agent_root
from arcui.schemas import (
    ChannelsResponse,
    ErrorResponse,
    SchedulesResponse,
    SessionEntry,
    SessionReplayResponse,
    SessionsListResponse,
    TasksResponse,
)

# Preview snippet cap for the Inbox row — enough to identify the message,
# never the whole body.
_PREVIEW_MAX = 240


def _peer_map(request: Request, agent_did: str | None) -> dict[str, str]:
    """Reverse-resolve teammate DM sessions → the peer's display name.

    A messaging session is named ``build_session_key(agent_did, peer_did)`` — a
    one-way hash, so the peer DID cannot be read back from the sid. Instead we
    forward-hash every known teammate's DID against this agent's DID and match:
    deterministic, no reversal, and it doubles as the ``messaging`` classifier.
    Human correspondents are not on the roster, so their sessions stay opaque.
    """
    provider = getattr(request.app.state, "roster_provider", None)
    if provider is None or not agent_did:
        return {}
    out: dict[str, str] = {}
    for entry in provider():
        peer_did = getattr(entry, "did", "")
        if not peer_did or peer_did == agent_did:
            continue
        label = getattr(entry, "display_name", "") or getattr(entry, "name", "") or peer_did
        out[build_session_key(agent_did, peer_did)] = label
    return out


def _current_session_key(request: Request, agent_did: str | None) -> str | None:
    """Resolve the calling viewer's CURRENT session key for this agent.

    The chat WebSocket writes to ``SessionRouter.current_session_key`` — the
    rotation-aware key that follows ``/new``. The session LIST must name that
    same key, or a client re-deriving the generation-0 base key would load a
    different (stale) conversation after a rotation. Derived from the same
    viewer token the socket and the rotate route use, so all three resolve the
    identical (agent, user) pair. Returns ``None`` on a read-only deployment
    (no ``session_router``) or an unknown agent — the list stays valid, just
    without a current marker.
    """
    router = getattr(request.app.state, "session_router", None)
    if router is None or not agent_did:
        return None
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not token:
        return None
    user_did = derive_viewer_did(token)
    key: str = router.current_session_key(agent_did, user_did)
    return key


def _session_kind(sid: str, peer_map: dict[str, str]) -> str:
    """Classify a session for the Inbox filter.

    A teammate DM (sid resolves to a known peer) is ``messaging``; a namespaced
    sid (``cli:run:…``, ``pulse:tick``, ``serve:stdin``) is its namespace; a bare
    key that matches no peer is a human ``chat``.
    """
    if sid in peer_map:
        return "messaging"
    if ":" in sid:
        return sid.split(":", 1)[0]
    return "chat"


def _extract_text(content: Any) -> str:
    """Flatten a message ``content`` (string or list of parts) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [str(p["text"]) for p in content if isinstance(p, dict) and "text" in p]
        return " ".join(parts)
    return str(content) if content is not None else ""


def _summarize_transcript(text: str) -> tuple[int | None, str | None, str | None, str | None]:
    """Fold a session JSONL into (message_count, last_role, last_text, last_ts).

    Counts only ``type == "message"`` lines (``checkpoint`` lines are loop
    metadata, not turns) and reads the last one's role/preview/timestamp.
    """
    count = 0
    last: dict[str, Any] | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "message":
            count += 1
            last = obj
    if last is None:
        return (count if count else None), None, None, None
    preview = _extract_text(last.get("content")).strip()[:_PREVIEW_MAX] or None
    return count, last.get("role"), preview, last.get("timestamp")


def _read_session_summary(
    agent_id: str, workspace: Path, sid: str
) -> tuple[int | None, str | None, str | None, str | None]:
    """Bounded read of one session transcript for the Inbox row.

    Degrades to all-``None`` when the file is missing, too large (fs_reader caps
    at 1 MiB), or blocked — the row still renders with its cheap metadata.
    """
    try:
        content = fs_reader.read_file(
            scope="agent",
            agent_id=agent_id,
            agent_root=workspace,
            rel_path=f"sessions/{sid}.jsonl",
            caller_did=_CALLER_DID,
        )
    except (FileNotFoundError, PathTraversalError, FileTooLargeError, OSError):
        return None, None, None, None
    return _summarize_transcript(content.content)


async def get_sessions(request: Request) -> JSONResponse:
    agent_id = request.path_params["id"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return JSONResponse(
            ErrorResponse(error="Agent not found").model_dump(mode="json"),
            status_code=404,
        )

    workspace = agent_root / "workspace"
    try:
        entries = fs_reader.list_tree(
            scope="agent",
            agent_id=agent_id,
            agent_root=workspace,
            rel_path="sessions",
            caller_did=_CALLER_DID,
            max_depth=1,
        )
    except PathTraversalError as exc:
        return JSONResponse(
            ErrorResponse(error=str(exc)).model_dump(mode="json"),
            status_code=400,
        )

    agent_did = _agent_did(request, agent_id)
    peer_map = _peer_map(request, agent_did)
    current_key = _current_session_key(request, agent_did)
    sessions: list[SessionEntry] = []
    for entry in entries:
        if entry.type != "file" or not entry.path.endswith(".jsonl"):
            continue
        sid = entry.path.rsplit("/", 1)[-1].removesuffix(".jsonl")
        count, last_role, last_text, last_ts = _read_session_summary(agent_id, workspace, sid)
        sessions.append(
            SessionEntry(
                sid=sid,
                path=entry.path,
                size=entry.size,
                mtime=entry.mtime,
                kind=_session_kind(sid, peer_map),
                counterpart=peer_map.get(sid),
                message_count=count,
                last_role=last_role,
                last_text=last_text,
                last_ts=last_ts,
                current=sid == current_key,
            )
        )
    sessions.sort(key=lambda s: float(s.mtime), reverse=True)
    return JSONResponse(
        SessionsListResponse(sessions=sessions, current_session_key=current_key).model_dump(
            mode="json"
        )
    )


async def get_session_replay(request: Request) -> JSONResponse:
    agent_id = request.path_params["id"]
    sid = request.path_params["sid"]
    if not _VALID_SID.match(sid):
        return JSONResponse(
            ErrorResponse(error="Invalid session id").model_dump(mode="json"),
            status_code=400,
        )

    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return JSONResponse(
            ErrorResponse(error="Agent not found").model_dump(mode="json"),
            status_code=404,
        )

    page, page_size, err = parse_pagination(request.query_params)
    if err is not None:
        return err

    workspace = agent_root / "workspace"
    try:
        content = fs_reader.read_file(
            scope="agent",
            agent_id=agent_id,
            agent_root=workspace,
            rel_path=f"sessions/{sid}.jsonl",
            caller_did=_CALLER_DID,
        )
    except FileNotFoundError:
        return JSONResponse(
            ErrorResponse(error="Session not found").model_dump(mode="json"),
            status_code=404,
        )
    except (PathTraversalError, FileTooLargeError) as exc:
        return JSONResponse(
            ErrorResponse(error=str(exc)).model_dump(mode="json"),
            status_code=400,
        )

    messages = _parse_jsonl(content.content)
    total = len(messages)
    if request.query_params.get("tail") in ("1", "true"):
        # A chat preload wants the NEWEST turns. Page 1 is the oldest slice,
        # so preloading it froze long conversations at their beginning —
        # everything past row page_size looked lost while sitting in the jsonl.
        window = messages[-page_size:]
        page = max(1, -(-total // page_size))
    else:
        start = (page - 1) * page_size
        window = messages[start : start + page_size]
    return JSONResponse(
        SessionReplayResponse(
            sid=sid,
            page=page,
            page_size=page_size,
            total=total,
            messages=window,
        ).model_dump(mode="json")
    )


def _parse_jsonl(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


# ---------------------------------------------------------------------------
# /api/agents/{id}/tasks, /api/agents/{id}/schedules
# ---------------------------------------------------------------------------


async def get_tasks(request: Request) -> JSONResponse:
    """GET /api/agents/{id}/tasks — this agent's arcstore-owned task rows."""
    agent_id = request.path_params["id"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return JSONResponse(
            ErrorResponse(error="Agent not found").model_dump(mode="json"),
            status_code=404,
        )
    owner_did = _agent_did(request, agent_id)
    rows = await request.app.state.observe.tasks(owner_did=owner_did)
    return JSONResponse(TasksResponse(tasks=rows).model_dump(mode="json"))


async def get_schedules(request: Request) -> JSONResponse:
    return await _read_json_array(
        request,
        rel_path="schedules.json",
        key="schedules",
        model_cls=SchedulesResponse,
    )


async def get_channels(request: Request) -> JSONResponse:
    """GET /api/agents/{id}/channels — delivery targets this agent has seen.

    Reads the agent's workspace ``channels.json`` (written by the turn path) and
    returns ``{target, label}`` pairs so arcui can populate a delivery dropdown.
    """
    agent_id = request.path_params["id"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return JSONResponse(
            ErrorResponse(error="Agent not found").model_dump(mode="json"),
            status_code=404,
        )
    try:
        content = fs_reader.read_file(
            scope="agent",
            agent_id=agent_id,
            agent_root=agent_root / "workspace",
            rel_path="channels.json",
            caller_did=_CALLER_DID,
        )
    except FileNotFoundError:
        return JSONResponse(ChannelsResponse(channels=[]).model_dump(mode="json"))
    except (PathTraversalError, FileTooLargeError) as exc:
        return JSONResponse(ErrorResponse(error=str(exc)).model_dump(mode="json"), status_code=400)
    try:
        parsed = json.loads(content.content)
    except json.JSONDecodeError:
        parsed = []
    channels = [
        {"target": str(e["target"]), "label": str(e.get("label") or e["target"])}
        for e in parsed
        if isinstance(e, dict) and e.get("target")
    ]
    return JSONResponse(ChannelsResponse(channels=channels).model_dump(mode="json"))


async def _read_json_array(
    request: Request,
    *,
    rel_path: str,
    key: str,
    model_cls: type[BaseModel],
) -> JSONResponse:
    agent_id = request.path_params["id"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return JSONResponse(
            ErrorResponse(error="Agent not found").model_dump(mode="json"),
            status_code=404,
        )

    workspace = agent_root / "workspace"
    try:
        content = fs_reader.read_file(
            scope="agent",
            agent_id=agent_id,
            agent_root=workspace,
            rel_path=rel_path,
            caller_did=_CALLER_DID,
        )
    except FileNotFoundError:
        return JSONResponse(model_cls(**{key: []}).model_dump(mode="json"))
    except (PathTraversalError, FileTooLargeError) as exc:
        return JSONResponse(
            ErrorResponse(error=str(exc)).model_dump(mode="json"),
            status_code=400,
        )

    try:
        parsed = json.loads(content.content)
    except json.JSONDecodeError:
        return JSONResponse(model_cls(**{key: []}).model_dump(mode="json"))
    if not isinstance(parsed, list):
        return JSONResponse(model_cls(**{key: []}).model_dump(mode="json"))
    return JSONResponse(model_cls(**{key: parsed}).model_dump(mode="json"))
