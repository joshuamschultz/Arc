"""``POST /api/agents/{id}/memory/promotion/run`` — "Run now" (SPEC-083 COMP-029, REQ-512).

Asks the RUNNING agent (``app.state.embedded_agent_cache``, keyed by DID) to run
its memory promotion sweep once now: a backfill over existing memory, after which
the nightly sweep sends only new or changed items. The route never builds a
second, offline agent; an agent not running in this process is ``503``.

Contract:

* Operator only. Body ``{}`` or ``{"max_items": int 1..5000}`` — nothing else.
* ``200`` returns the sweep status and its six counts only, never memory content.
  A non-``completed`` status (``tier_forbidden``, ``disabled``, ...) is a result.
* An overlapping nightly sweep is waited for (arcmemory's per-agent lock), never
  reported as busy.

Audit: every attempt — accepted, refused or crashed — emits one
``memory.promotion.manual_run`` record (target ``agent:<id>``, the cap in
``detail``; outcome ``applied`` / ``denied`` / ``failed``). Refusals are logged
too (NIST AU-2/AU-12). No record carries memory content or an error message.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

import arcagent
from starlette.requests import Request
from starlette.responses import JSONResponse

from arcui.audit import emit_mutation_audit
from arcui.routes.agent_detail._common import _agent_did, _agent_root
from arcui.routes.agent_detail.config_files import BodyTooLargeError, _error, read_json_object

logger = logging.getLogger("arcui.routes.agent_detail.memory_promotion_run")

_AUDIT_OPERATION = "memory.promotion.manual_run"
_COUNTS = ("evaluated", "promoted", "kept_private", "blocked_secret", "too_large", "deferred")
_MAX_ITEMS = arcagent.MEMORY_PROMOTION_MAX_ITEMS


class _RunRefusedError(ValueError):
    """The request is refused; ``status`` is the HTTP code, the message is safe to show."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


def _max_items(body: Mapping[str, Any]) -> int | None:
    """The requested cap, or ``None``; raises when the body is anything but ``{max_items?}``."""
    extra = sorted(set(body) - {"max_items"})
    if extra:
        raise _RunRefusedError(f"unknown field(s): {', '.join(extra)}", 422)
    cap: object = body.get("max_items")
    if cap is None:
        return None
    if isinstance(cap, int) and not isinstance(cap, bool) and 1 <= cap <= _MAX_ITEMS:
        return cap
    raise _RunRefusedError(f"max_items must be between 1 and {_MAX_ITEMS}", 422)


async def _read_body(request: Request) -> dict[str, Any]:
    """The JSON object body (empty body = ``{}``); raises on anything else."""
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        body = await read_json_object(request)
    except BodyTooLargeError as exc:
        raise _RunRefusedError("Request body too large", 413) from exc
    if body is None:
        raise _RunRefusedError("Body must be a JSON object", 400)
    return body


def _wire(result: Mapping[str, object]) -> dict[str, object]:
    """Status + the six counts; any other key the agent side returned is dropped."""
    wire: dict[str, object] = {"status": str(result.get("status", ""))}
    for name in _COUNTS:
        value = result.get(name, 0)
        wire[name] = int(value) if isinstance(value, int) else 0
    return wire


def _running_agent(request: Request, agent_id: str) -> Any | None:
    cache = getattr(request.app.state, "embedded_agent_cache", None)
    did = _agent_did(request, agent_id)
    return cache.get(did) if cache is not None and did is not None else None


async def run_memory_promotion(request: Request) -> JSONResponse:
    """POST — run promotion once now on the running agent. Operator only; audited."""
    agent_id = request.path_params["id"]
    audit = _Audit(request, agent_id)
    if getattr(request.state, "role", None) != "operator":
        audit.record("denied", {"reason": "operator role required"})
        return _error("Operator role required", 403)
    if _agent_root(request, agent_id) is None:
        audit.record("denied", {"reason": "agent not found"})
        return _error("Agent not found", 404)
    try:
        cap = _max_items(await _read_body(request))
    except _RunRefusedError as exc:
        audit.record("denied", {"reason": str(exc)})
        return _error(str(exc), exc.status)
    audit.cap = cap

    agent = _running_agent(request, agent_id)
    run = getattr(agent, "run_memory_promotion", None)
    if run is None:
        audit.record("failed", {"reason": "agent is not running in this process"})
        return _error("agent is not running in this process", 503)
    return await _run(run, audit, cap)


async def _run(run: Any, audit: _Audit, cap: int | None) -> JSONResponse:
    try:
        result = await run(max_items=cap)
    except arcagent.CapabilityUnavailableError:
        audit.record("failed", {"reason": "memory promotion unavailable"})
        return _error("this agent's memory cannot run promotion", 503)
    except Exception as exc:  # reason: never leak a message that may carry content or paths
        logger.warning("manual promotion run failed (%s)", type(exc).__name__)
        audit.record("failed", {"error": type(exc).__name__})
        return _error("promotion run failed", 500)
    wire = _wire(result)
    audit.record("applied", wire)
    return JSONResponse(wire)


class _Audit:
    """One ``memory.promotion.manual_run`` record per attempt; the cap is always in detail."""

    def __init__(self, request: Request, agent_id: str) -> None:
        self._request = request
        self._target = f"agent:{agent_id}"
        self.cap: int | None = None

    def record(self, outcome: str, extra: Mapping[str, object]) -> None:
        detail = json.dumps({"max_items": self.cap, **extra}, sort_keys=True)
        emit_mutation_audit(
            self._request,
            target=self._target,
            operation=_AUDIT_OPERATION,
            outcome=outcome,
            detail=detail,
        )


__all__ = ["run_memory_promotion"]
