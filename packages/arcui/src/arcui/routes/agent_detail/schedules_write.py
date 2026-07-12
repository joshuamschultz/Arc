"""``PATCH /api/agents/{id}/schedules/{sid}`` — operator-gated schedule edit.

The agent's scheduler engine re-reads ``schedules.json`` from disk on every
timer tick (``SchedulerEngine._timer_loop`` calls ``store.load()`` each
iteration), so an atomic write to that file is picked up live on the next tick
— no IPC to the agent process is needed. This route therefore edits the file
in place, mirroring ``files_write.py``'s confined-write shape and ``tasks.py``'s
operator-gate → validate → write → audit shape.

arcui holds no agent runtime, so it does not import the ``arcagent`` scheduler
model; it validates the allowlisted edits structurally here (the operator is a
trusted human — same posture as the workspace file editor, which gates on role
rather than re-running the agent-side injection guard). Cron expressions are
checked with ``croniter``. Interval floor / timeout ceiling / prompt length use
the scheduler module's documented default limits (arcui cannot see the agent's
``[modules.scheduler.config]`` overrides).
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from croniter import croniter
from starlette.requests import Request
from starlette.responses import JSONResponse

from arcui.audit import emit_mutation_audit
from arcui.routes.agent_detail._common import _agent_root
from arcui.schemas import ErrorResponse

# Default limits, matching arcagent scheduler module defaults (SchedulerConfig).
_MAX_PROMPT_LENGTH = 500
_MIN_INTERVAL_SECONDS = 60
_MAX_TIMEOUT_SECONDS = 3600

# Fields a PATCH may write. Timing fields are gated on the schedule's own type
# below; id / type / metadata are managed by the scheduler engine, never a
# client-supplied key.
_TYPE_TIMING_FIELD = {"cron": "expression", "interval": "every_seconds", "once": "at"}

# Owner read/write only (0o600), matching ScheduleStore's on-disk permissions.
_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse(ErrorResponse(error=message).model_dump(mode="json"), status_code=status)


def _is_operator(request: Request) -> bool:
    return getattr(request.state, "role", None) == "operator"


async def _json_body(request: Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except Exception:  # reason: malformed body is a client error, not a 500
        return None
    return body if isinstance(body, dict) else None


def _collect_edits(body: dict[str, Any], schedule_type: str) -> dict[str, Any]:
    """Pull only the editable fields for this schedule's type out of ``body``.

    ``enabled`` / ``prompt`` / ``timeout_seconds`` always apply; the timing
    field is whichever one matches the schedule's type, so an ``expression`` on
    an interval schedule (or vice-versa) is silently dropped.
    """
    edits: dict[str, Any] = {}
    for key in ("enabled", "prompt", "timeout_seconds"):
        if key in body:
            edits[key] = body[key]
    timing = _TYPE_TIMING_FIELD.get(schedule_type)
    if timing is not None and timing in body:
        edits[timing] = body[timing]
    return edits


def _validate_edits(edits: dict[str, Any]) -> str | None:
    """Return an error message for the first invalid field, else None."""
    if "enabled" in edits and not isinstance(edits["enabled"], bool):
        return "enabled must be a boolean"
    if "prompt" in edits:
        prompt = edits["prompt"]
        if not isinstance(prompt, str) or not prompt.strip():
            return "prompt must be a non-empty string"
        if len(prompt) > _MAX_PROMPT_LENGTH:
            return f"prompt exceeds maximum length ({_MAX_PROMPT_LENGTH})"
    if "timeout_seconds" in edits:
        timeout = edits["timeout_seconds"]
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 1:
            return "timeout_seconds must be a positive integer"
        if timeout > _MAX_TIMEOUT_SECONDS:
            return f"timeout_seconds exceeds maximum ({_MAX_TIMEOUT_SECONDS})"
    if "expression" in edits and not croniter.is_valid(str(edits["expression"])):
        return f"invalid cron expression: {edits['expression']}"
    if "every_seconds" in edits:
        every = edits["every_seconds"]
        if not isinstance(every, int) or isinstance(every, bool) or every < _MIN_INTERVAL_SECONDS:
            return f"every_seconds must be an integer >= {_MIN_INTERVAL_SECONDS}"
    if "at" in edits:
        try:
            datetime.fromisoformat(str(edits["at"]))
        except ValueError:
            return "at must be an ISO 8601 datetime"
    return None


def _atomic_write_json(path: Path, data: list[Any]) -> None:
    """Atomically replace ``path`` with ``data`` (tempfile + fsync + os.replace).

    The scheduler reads this file concurrently every tick; an atomic replace
    guarantees it never observes a torn write.
    """
    payload = json.dumps(data, indent=2).encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.write(fd, payload)
        os.fsync(fd)
        os.close(fd)
        os.chmod(tmp, _FILE_MODE)
        os.replace(tmp, str(path))
    except Exception:  # reason: clean up the temp file, then re-raise
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


async def patch_schedule(request: Request) -> JSONResponse:
    """PATCH /api/agents/{id}/schedules/{sid} — edit a schedule (operator only)."""
    agent_id = request.path_params["id"]
    sid = request.path_params["sid"]
    target = f"schedule:{sid}"

    if not _is_operator(request):
        emit_mutation_audit(
            request,
            target=target,
            operation="schedule.update",
            outcome="denied",
            detail="viewer role",
        )
        return _error("operator_role_required", 403)

    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return _error("Agent not found", 404)

    body = await _json_body(request)
    if body is None:
        return _error("expected a JSON object body", 400)

    path = agent_root / "workspace" / "schedules.json"
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _error("not found", 404)
    except (json.JSONDecodeError, OSError) as exc:
        return _error(f"could not read schedules: {exc}", 400)
    if not isinstance(entries, list):
        return _error("not found", 404)

    index = next(
        (i for i, e in enumerate(entries) if isinstance(e, dict) and e.get("id") == sid),
        None,
    )
    if index is None:
        return _error("not found", 404)

    edits = _collect_edits(body, str(entries[index].get("type", "")))
    if not edits:
        return _error("no editable fields", 400)
    invalid = _validate_edits(edits)
    if invalid is not None:
        emit_mutation_audit(
            request, target=target, operation="schedule.update", outcome="denied", detail=invalid
        )
        return _error(invalid, 400)

    entries[index] = {**entries[index], **edits}
    try:
        _atomic_write_json(path, entries)
    except OSError as exc:
        emit_mutation_audit(
            request, target=target, operation="schedule.update", outcome="error", detail=str(exc)
        )
        return _error(f"could not write schedules: {exc}", 400)

    emit_mutation_audit(request, target=target, operation="schedule.update", outcome="applied")
    return JSONResponse(entries[index])


__all__ = ["patch_schedule"]
