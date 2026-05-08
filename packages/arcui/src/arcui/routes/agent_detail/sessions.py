"""`/api/agents/{id}/sessions[/{sid}]` + tasks/schedules route handlers."""

from __future__ import annotations

import json
from typing import Any

from arcgateway import fs_reader
from arcgateway.fs_reader import FileTooLargeError, PathTraversalError
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse

from arcui.query_validators import parse_pagination
from arcui.routes.agent_detail._common import _CALLER_DID, _VALID_SID, _agent_root
from arcui.schemas import (
    ErrorResponse,
    SchedulesResponse,
    SessionEntry,
    SessionReplayResponse,
    SessionsListResponse,
    TasksResponse,
)


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

    sessions: list[SessionEntry] = []
    for entry in entries:
        if entry.type != "file" or not entry.path.endswith(".jsonl"):
            continue
        sid = entry.path.rsplit("/", 1)[-1].removesuffix(".jsonl")
        sessions.append(
            SessionEntry(
                sid=sid,
                path=entry.path,
                size=entry.size,
                mtime=entry.mtime,
            )
        )
    sessions.sort(key=lambda s: float(s.mtime), reverse=True)
    return JSONResponse(SessionsListResponse(sessions=sessions).model_dump(mode="json"))


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
    start = (page - 1) * page_size
    end = start + page_size
    return JSONResponse(
        SessionReplayResponse(
            sid=sid,
            page=page,
            page_size=page_size,
            total=total,
            messages=messages[start:end],
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
    return await _read_json_array(
        request,
        rel_path="tasks.json",
        key="tasks",
        model_cls=TasksResponse,
    )


async def get_schedules(request: Request) -> JSONResponse:
    return await _read_json_array(
        request,
        rel_path="schedules.json",
        key="schedules",
        model_cls=SchedulesResponse,
    )


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
