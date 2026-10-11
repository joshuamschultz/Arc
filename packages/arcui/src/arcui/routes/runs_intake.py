"""Run intake: start an agent run over HTTP and read it back (SPEC-088 COMP-001).

``POST /api/runs`` records one intake task row that the agent's tasks dispatch
loop runs under the returned ``run_id``; ``GET /api/runs/{run_id}`` projects
that row back. The list ``GET /api/runs`` stays in ``observe_run`` and outside
``APP_ROUTES``, so apps can start and read runs but never enumerate them.

POST pipeline, each step refusing before the next reveals more:

1. ``Idempotency-Key`` (1..128 safe characters), JSON body (64 KiB), the
   :class:`IntakeRequest` fields (400), caller metadata size (413);
2. the agent (handle or DID) resolved through the roster (404);
3. ``runs:start`` on that agent (403, scope before any agent detail);
4. ``callback_url``: notifications present (503) and allowlisted (400);
5. the agent accepts runs: its ``arcagent.toml`` enables tasks dispatch (409);
6. ``run_id = uuid5(namespace, principal DID + key)``, created only when
   absent: a replay of the same request is 200, a different one 409.

Every outcome writes exactly one ``run.intake`` audit row. An app refused a
read gets one ``run.read`` row and the same 404 whether or not the run exists.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import tomllib
import uuid
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode

from arcstore.tasks import Task, TaskConflictError, TaskStore
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.acting import acting_identity, control_refusal
from arcui.audit import emit_mutation_audit
from arcui.principals import Scope, require_scope

logger = logging.getLogger(__name__)

INTAKE_OPERATION = "run.intake"
READ_OPERATION = "run.read"
INTAKE_TAG = "intake"
TASK_ID_PREFIX = "intake-"

# Fixed forever: changing it would give every replayed key a new run_id.
RUN_ID_NAMESPACE = uuid.UUID("e0fb9069-ada1-4fd4-a2d5-40fec85b4158")

MAX_BODY_BYTES = 64 * 1024
MAX_METADATA_BYTES = 16 * 1024
MAX_METADATA_DEPTH = 8
TITLE_CHARS = 120
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_UUID_TEXT = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

_STATUS: dict[str, str] = {
    "backlog": "queued",
    "todo": "queued",
    "in_progress": "running",
    "review": "running",
    "done": "done",
}

NOT_FOUND_BODY: dict[str, str] = {"error": "not found"}


class _IntakeRefusedError(Exception):
    """One refused intake step: HTTP status, wire error code and audit fields."""

    def __init__(self, status: int, code: str, detail: str, *, target: str = "runs") -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.detail = detail
        self.target = target

    @property
    def outcome(self) -> str:
        return "error" if self.status >= 500 else "denied"


def _depth(value: Any) -> int:
    """Nesting depth of JSON containers; a flat object is depth 1."""
    if isinstance(value, dict):
        return 1 + max((_depth(v) for v in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((_depth(v) for v in value), default=0)
    return 0


def _canonical(value: Any) -> bytes:
    """Key-sorted compact JSON: the same request always hashes the same."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


class IntakeRequest(BaseModel):
    """The only fields ``POST /api/runs`` accepts (REQ-589, REQ-590)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent: str = Field(min_length=1, max_length=256)
    task: str = Field(min_length=1)
    metadata: dict[str, Any] | None = None
    correlation_id: str | None = Field(default=None, pattern=_SAFE_TOKEN.pattern)
    run_purpose: Literal["manual", "message"] = "manual"
    callback_url: str | None = Field(default=None, min_length=1, max_length=2048)

    @field_validator("metadata")
    @classmethod
    def _bounded_depth(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None and _depth(value) > MAX_METADATA_DEPTH:
            raise ValueError(f"metadata nests deeper than {MAX_METADATA_DEPTH}")
        return value


def run_id_for(principal_did: str, idempotency_key: str) -> str:
    """The run id one principal gets for one key; two principals never share it."""
    return str(uuid.uuid5(RUN_ID_NAMESPACE, f"{principal_did}\x1f{idempotency_key}"))


def accepts_runs(agent_root: Path) -> bool:
    """True only when the agent's tasks module is on and its dispatch loop runs.

    Reads the layout the tasks module loads: ``[modules.tasks]`` ``enabled``
    (default true, as ``ModuleEntry``) and ``[modules.tasks.config]``
    ``dispatch`` (a real ``true``). A missing or malformed file, or any value of
    the wrong type, is False (fail closed). Read on every call, so an operator
    turning dispatch off stops intake on the next request.
    """
    try:
        data = tomllib.loads((agent_root / "arcagent.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return False
    modules = data.get("modules")
    tasks = modules.get("tasks") if isinstance(modules, dict) else None
    if not isinstance(tasks, dict) or tasks.get("enabled", True) is not True:
        return False
    config = tasks.get("config", {})
    return isinstance(config, dict) and config.get("dispatch") is True


def intake_metadata(task: Task, run_id: str) -> dict[str, Any] | None:
    """The ``metadata.intake`` block when ``task`` is the intake row of ``run_id``."""
    intake = task.metadata.get("intake")
    if isinstance(intake, dict) and intake.get("run_id") == run_id and INTAKE_TAG in task.tags:
        return intake
    return None


async def load_intake_row(request: Request, run_id: str) -> Task | None:
    """The intake row for ``run_id``, or None (no store, no row, or not an intake row)."""
    store: TaskStore | None = getattr(request.app.state, "task_store", None)
    if store is None or not _UUID_TEXT.match(run_id):
        return None
    row = await store.get(f"{TASK_ID_PREFIX}{run_id}")
    return row if row is not None and intake_metadata(row, run_id) is not None else None


def run_read_refusal(
    request: Request, run_id: str, agent_did: str | None, *, operation: str
) -> JSONResponse | None:
    """None when the caller may read a run of ``agent_did``; else one audited 404.

    ``require_scope`` decides and writes the one denial row: sessions read any
    run, an app needs ``runs:read`` on ``agent_did`` (None, an unresolvable or
    unknown run, is refused). The refusal is answered with the same 404 body
    as a missing run, so an app cannot probe which runs exist (REQ-595, 598).
    """
    denial = require_scope(
        request, Scope.RUNS_READ, agent_did=agent_did, target=f"run:{run_id}", operation=operation
    )
    return None if denial is None else JSONResponse(NOT_FOUND_BODY, status_code=404)


# --- POST /api/runs -------------------------------------------------------------


def _idempotency_key(request: Request) -> str:
    key = request.headers.get("idempotency-key")
    if key is None or not _SAFE_TOKEN.match(key):
        raise _IntakeRefusedError(
            400, "invalid_idempotency_key", "Idempotency-Key missing or malformed"
        )
    return key


async def _read_json(request: Request) -> Any:
    if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
        raise _IntakeRefusedError(400, "invalid_body", "content type is not application/json")
    chunks = bytearray()
    async for chunk in request.stream():
        chunks.extend(chunk)
        if len(chunks) > MAX_BODY_BYTES:
            raise _IntakeRefusedError(413, "body_too_large", f"body over {MAX_BODY_BYTES} bytes")
    try:
        return json.loads(chunks)
    except ValueError as exc:
        raise _IntakeRefusedError(400, "invalid_body", "body is not JSON") from exc


def _parse(payload: Any) -> IntakeRequest:
    try:
        body = IntakeRequest.model_validate(payload)
    except ValidationError as exc:
        fields = ",".join(str(err["loc"][0]) for err in exc.errors() if err["loc"]) or "body"
        raise _IntakeRefusedError(400, "invalid_request", f"invalid fields: {fields}") from exc
    if body.metadata is not None and len(_canonical(body.metadata)) > MAX_METADATA_BYTES:
        raise _IntakeRefusedError(
            413, "metadata_too_large", f"metadata over {MAX_METADATA_BYTES} bytes"
        )
    return body


async def _resolve_agent(request: Request, agent: str) -> tuple[str, Path]:
    provider = getattr(request.app.state, "roster_provider", None)
    rows = await asyncio.to_thread(provider) if provider is not None else []
    for row in rows:
        if agent in (row.agent_id, row.did):
            return str(row.did), Path(row.workspace_path)
    raise _IntakeRefusedError(404, "agent_not_found", "agent not in roster")


async def _check_callback(request: Request, url: str, target: str) -> None:
    policy = getattr(request.app.state, "callback_policy", None)
    dispatcher = getattr(request.app.state, "callback_dispatcher", None)
    if policy is None or dispatcher is None:
        raise _IntakeRefusedError(
            503, "notifications_unavailable", "callback components absent", target=target
        )
    try:
        # The policy resolves DNS and reads the allowlist authority: off the loop.
        await asyncio.to_thread(policy.check, url)
    except ValueError as exc:
        raise _IntakeRefusedError(
            400, "callback_not_allowed", "callback origin refused", target=target
        ) from exc


def _intake_task(request: Request, body: IntakeRequest, agent_did: str, run_id: str) -> Task:
    identity = acting_identity(request)
    fields = {
        "id": f"{TASK_ID_PREFIX}{run_id}",
        "title": body.task[:TITLE_CHARS],
        "description": body.task,
        "owner_did": agent_did,
        "creator_did": identity.actor_did,
        "status": "todo",
        "max_attempts": 1,
        "tags": [INTAKE_TAG],
        "metadata": {
            "intake": {
                "run_id": run_id,
                "run_purpose": body.run_purpose,
                "correlation_id": body.correlation_id,
                "callback_url": body.callback_url,
                "on_behalf_of": identity.on_behalf_of,
                "via_app": identity.via_app,
            },
            "app": body.metadata or {},
        },
    }
    refs = bool(getattr(request.app.state, "allow_external_task_refs", False))
    try:
        return Task.model_validate(fields, context={"allow_external_refs": refs})
    except ValidationError as exc:
        raise _IntakeRefusedError(
            400, "invalid_task", "task text refused", target=f"run:{run_id}"
        ) from exc


def _request_digest(body: IntakeRequest, agent_did: str) -> str:
    material = {**body.model_dump(mode="json"), "agent": agent_did}
    return hashlib.sha256(_canonical(material)).hexdigest()


def _links(request: Request, run_id: str, agent_did: str) -> dict[str, Any]:
    query = urlencode({"agent": agent_did, "run_id": run_id})
    events: dict[str, str] = {"stream": f"/api/work-events/stream?{query}"}
    if getattr(request.app.state, "callback_dispatcher", None) is None:
        events["notifications"] = "unavailable"
    return {
        "run": f"/api/runs/{run_id}",
        "timeline": f"/api/runs/{run_id}/timeline",
        "events": events,
    }


def run_status(task: Task) -> str:
    """The public run status for an intake row's task status (REQ-595)."""
    if task.status == "failed":
        return "cancelled" if task.resolution == "cancelled" else "failed"
    return _STATUS[task.status]


async def _start(request: Request) -> JSONResponse:
    key = _idempotency_key(request)
    body = _parse(await _read_json(request))
    agent_did, agent_root = await _resolve_agent(request, body.agent)
    denial = control_refusal(
        request,
        Scope.RUNS_START,
        agent_did,
        target=f"agent:{agent_did}",
        operation=INTAKE_OPERATION,
    )
    if denial is not None:
        return denial
    # control_refusal passed, so a principal is bound to this request.
    run_id = run_id_for(request.state.principal.did, key)
    target = f"run:{run_id}"
    if body.callback_url is not None:
        await _check_callback(request, body.callback_url, target)
    if not await asyncio.to_thread(accepts_runs, agent_root):
        raise _IntakeRefusedError(
            409, "agent_not_accepting_runs", "tasks dispatch is off", target=target
        )
    task = _intake_task(request, body, agent_did, run_id)
    store: TaskStore = request.app.state.task_store
    try:
        stored, created = await store.create_if_absent(
            task, request_digest=_request_digest(body, agent_did)
        )
    except TaskConflictError as exc:
        raise _IntakeRefusedError(
            409, "idempotency_conflict", "key reused for another request", target=target
        ) from exc
    emit_mutation_audit(
        request,
        target=target,
        operation=INTAKE_OPERATION,
        outcome="applied",
        detail=f"run_id={run_id} agent={agent_did} replayed={not created}",
    )
    payload: dict[str, Any] = {
        "run_id": run_id,
        "status": run_status(stored),
        "links": _links(request, run_id, agent_did),
    }
    if not created:
        payload["replayed"] = True
    return JSONResponse(payload, status_code=202 if created else 200)


async def start_run(request: Request) -> JSONResponse:
    """POST /api/runs — record one run request for an agent (REQ-588..594)."""
    try:
        return await _start(request)
    except _IntakeRefusedError as refused:
        emit_mutation_audit(
            request,
            target=refused.target,
            operation=INTAKE_OPERATION,
            outcome=refused.outcome,
            detail=f"{refused.code}: {refused.detail}",
        )
        return JSONResponse({"error": refused.code}, status_code=refused.status)


# --- GET /api/runs/{run_id} -----------------------------------------------------


def _projection(row: Task, run_id: str) -> dict[str, Any]:
    intake = intake_metadata(row, run_id) or {}
    app_metadata = row.metadata.get("app")
    return {
        "run_id": run_id,
        "agent_did": row.owner_did,
        "status": run_status(row),
        "correlation_id": intake.get("correlation_id"),
        "run_purpose": intake.get("run_purpose"),
        "metadata": app_metadata if isinstance(app_metadata, dict) else {},
        "result": row.output,
        "error": row.last_error,
        "created_at": row.created_at,
        "started_at": row.started_at,
        "completed_at": row.completed_at,
    }


async def get_run(request: Request) -> JSONResponse:
    """GET /api/runs/{run_id} — the run's status projection (REQ-595).

    An app needs ``runs:read`` on the intake row's agent; anything else,
    including an unknown run, is the same audited 404. Sessions read any run.
    """
    run_id = request.path_params["run_id"]
    row = await load_intake_row(request, run_id)
    agent_did = row.owner_did if row is not None else None
    denial = run_read_refusal(request, run_id, agent_did, operation=READ_OPERATION)
    if denial is not None:
        return denial
    if row is None:
        return JSONResponse(NOT_FOUND_BODY, status_code=404)
    return JSONResponse(_projection(row, run_id))


routes = [
    Route("/api/runs", start_run, methods=["POST"]),
    Route("/api/runs/{run_id}", get_run, methods=["GET"]),
]
