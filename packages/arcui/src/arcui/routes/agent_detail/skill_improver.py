"""`/api/agents/{id}/skills/{skill_name}/` — arcskill operator controls (alpha-2 P8).

* ``GET  improver``          — the read model: lifecycle state, candidates with judge
  scores, usage traces, golden-suite size, and recent gate verdicts with reasons.
  Read from the agent workspace by ``arcskill.improver.ImproverStateReader``; it
  needs no running agent and writes nothing.
* ``POST improve``           — improve now. ``?dry_run=1`` (the default) returns the
  proposed diff + gate verdict and writes nothing. ``?dry_run=0`` with
  ``{"confirm": true}`` applies — the previewed candidate when ``preview_id`` is
  given — through the agent's own EvalGate, authorization ladder and
  ``apply_result``; the route then reloads the agent.
* ``POST evals/run``         — run the golden suite now; pass/fail per case.
* ``POST evals/regen``       — regenerate the machine-authored anchors
  (``{"confirm": true}``).

The POSTs are operator-only and go to the RUNNING agent
(``app.state.embedded_agent_cache``) through ``ArcAgent.skill_adapter()`` — the
route never builds a second, offline improver. The adapter bounds the work
(single flight per skill, a wall-clock ceiling, an iteration-bounded optimize
pass), and every call stays async, so the event loop is never blocked.

Audit: every POST attempt — accepted, refused or failed — writes one record
(``skill.improve.preview`` / ``skill.improve.apply`` / ``skill.evals.run`` /
``skill.evals.regen``) whose outcome is the control's status. Records carry ids
and verdicts, never a skill body or an error message.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from arcskill.improver import ImproverStateReader
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from arcui.audit import emit_mutation_audit
from arcui.routes.agent_detail._common import _agent_root, logger
from arcui.routes.agent_detail.capabilities import _live_agent
from arcui.routes.agent_detail.config_files import BodyTooLargeError, read_json_object
from arcui.routes.agent_detail.skill_versions import _skill_dir
from arcui.schemas import ErrorResponse

#: Control statuses that are refusals or failures rather than results.
_STATUS_CODES = {
    "unavailable": 503,
    "not_found": 404,
    "busy": 409,
    "stale_preview": 409,
    "preview_expired": 409,
    "timeout": 504,
}
_AUDIT_KEYS = ("candidate_id", "preview_id", "total", "passed", "failed", "adopted")


# ---------------------------------------------------------------------------
# Wire models — the adapter speaks primitive dicts; only these fields leave arcui
# ---------------------------------------------------------------------------


class _Wire(BaseModel):
    model_config = ConfigDict(extra="ignore")


class GateVerdict(_Wire):
    accepted: bool
    reason: str = ""
    before_pass: int = 0
    after_pass: int = 0
    newly_passing: int = 0


class ImproveResult(_Wire):
    """Body of ``POST .../improve`` — a preview, an apply outcome, or a refusal."""

    status: str
    skill_name: str
    reason: str = ""
    preview_id: str | None = None
    candidate_id: str | None = None
    generation: int | None = None
    diff: str | None = None
    scores: dict[str, float] | None = None
    seed_scores: dict[str, float] | None = None
    stop_reason: str | None = None
    iterations_run: int | None = None
    gate: GateVerdict | None = None
    approval_required: bool | None = None


class EvalCaseResult(_Wire):
    case_id: str
    passed: bool
    detail: str = ""
    gate_type: str = "exact_match"
    provenance: str = ""


class QuarantinedCase(_Wire):
    nodeid: str
    reason: str


class EvalsResult(_Wire):
    """Body of ``POST .../evals/run`` and ``POST .../evals/regen``."""

    status: str
    skill_name: str
    reason: str = ""
    total: int | None = None
    passed: int | None = None
    failed: int | None = None
    cases: list[EvalCaseResult] | None = None
    adopted: int | None = None
    quarantined: list[QuarantinedCase] | None = None


# ---------------------------------------------------------------------------
# GET improver
# ---------------------------------------------------------------------------


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse(ErrorResponse(error=message).model_dump(mode="json"), status_code=status)


async def get_skill_improver(request: Request) -> JSONResponse:
    """GET .../skills/{skill_name}/improver — the improver read model (no writes)."""
    agent_id = request.path_params["id"]
    skill_name = request.path_params["skill_name"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return _error("Agent not found", 404)
    skill_dir = await _skill_dir(request, agent_root, skill_name)
    if isinstance(skill_dir, JSONResponse):
        return skill_dir
    if skill_dir is None:
        return _error(f"skill {skill_name!r} not found", 404)
    reader = ImproverStateReader(agent_root / "workspace")
    try:
        state = await asyncio.to_thread(reader.read, skill_name, skill_dir=skill_dir)
    except ValueError:
        return _error("Invalid skill name", 400)
    live = _live_agent(request, agent_id) is not None
    return JSONResponse({**state.to_dict(), "live": live})


# ---------------------------------------------------------------------------
# POST controls
# ---------------------------------------------------------------------------


class _Control:
    """One operator control call: validate, ask the running agent, audit, answer."""

    def __init__(self, request: Request, operation: str) -> None:
        self.request = request
        self.operation = operation
        self.agent_id: str = request.path_params["id"]
        self.skill_name: str = request.path_params["skill_name"]

    def audit(self, outcome: str, extra: dict[str, Any] | None = None) -> None:
        emit_mutation_audit(
            self.request,
            target=f"skill://{self.agent_id}/{self.skill_name}",
            operation=self.operation,
            outcome=outcome,
            detail=json.dumps(extra or {}, sort_keys=True, default=str),
        )

    def refuse(self, outcome: str, message: str, status: int) -> JSONResponse:
        self.audit(outcome, {"reason": message})
        return _error(message, status)

    async def preflight(self) -> tuple[Any, dict[str, Any]] | JSONResponse:
        """Role, agent, skill, body, running agent — in that order; refusals are audited."""
        if getattr(self.request.state, "role", None) != "operator":
            return self.refuse("denied", "Operator role required", 403)
        agent_root = _agent_root(self.request, self.agent_id)
        if agent_root is None:
            return self.refuse("denied", "Agent not found", 404)
        skill_dir = await _skill_dir(self.request, agent_root, self.skill_name)
        if isinstance(skill_dir, JSONResponse):
            self.audit("failed", {"reason": "skill revision unavailable"})
            return skill_dir
        if skill_dir is None:
            return self.refuse("denied", f"skill {self.skill_name!r} not found", 404)
        body = await _read_body(self.request)
        if body is None:
            return self.refuse("denied", "Body must be a JSON object", 400)
        agent = _live_agent(self.request, self.agent_id)
        if agent is None or not hasattr(agent, "skill_adapter"):
            return self.refuse("failed", "agent is not running in this process", 503)
        return agent, body

    async def call(self, agent: Any, method: str, **kwargs: Any) -> dict[str, Any] | JSONResponse:
        try:
            adapter = await agent.skill_adapter()
            result: Any = await getattr(adapter, method)(skill_name=self.skill_name, **kwargs)
        except Exception as exc:  # reason: never leak a message that may carry paths or content
            logger.warning("skill control %s failed (%s)", self.operation, type(exc).__name__)
            self.audit("failed", {"error": type(exc).__name__})
            return _error("skill control failed", 500)
        if not isinstance(result, dict) or not isinstance(result.get("status"), str):
            self.audit("failed", {"error": "malformed adapter result"})
            return _error("skill control failed", 500)
        return result

    def answer(self, model: type[_Wire], result: dict[str, Any]) -> JSONResponse:
        try:
            wire = model.model_validate(result)
        except ValidationError:
            self.audit("failed", {"error": "malformed adapter result"})
            return _error("skill control failed", 500)
        status = str(result["status"])
        self.audit(status, _audit_extra(result))
        code = _STATUS_CODES.get(status, 200)
        payload = wire.model_dump(mode="json", exclude_none=True)
        if code != 200:
            # Clients read a refusal's message from ``error`` (the ErrorResponse key).
            payload["error"] = str(result.get("reason") or status)
        return JSONResponse(payload, status_code=code)


async def _read_body(request: Request) -> dict[str, Any] | None:
    """The JSON object body (empty = ``{}``); ``None`` for anything else."""
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        return await read_json_object(request)
    except BodyTooLargeError:
        return None


def _audit_extra(result: dict[str, Any]) -> dict[str, Any]:
    extra = {key: result[key] for key in _AUDIT_KEYS if key in result}
    gate = result.get("gate")
    if isinstance(gate, dict):
        extra["gate_accepted"] = gate.get("accepted")
    return extra


def _confirmed(body: dict[str, Any]) -> bool:
    return body.get("confirm") is True


async def post_skill_improve(request: Request) -> JSONResponse:
    """POST .../skills/{skill_name}/improve[?dry_run=0|1] — improve now (operator)."""
    dry_run = request.query_params.get("dry_run", "1") != "0"
    control = _Control(request, "skill.improve.preview" if dry_run else "skill.improve.apply")
    pre = await control.preflight()
    if isinstance(pre, JSONResponse):
        return pre
    agent, body = pre
    preview_id = body.get("preview_id")
    if preview_id is not None and not isinstance(preview_id, str):
        return control.refuse("denied", "'preview_id' must be a string", 400)
    if not dry_run and not _confirmed(body):
        return control.refuse(
            "denied", "apply requires explicit confirmation: 'confirm': true", 400
        )
    result = await control.call(
        agent, "improve_now", dry_run=dry_run, preview_id=None if dry_run else preview_id
    )
    if isinstance(result, JSONResponse):
        return result
    if result["status"] == "applied":
        try:
            await agent.reload_or_raise()
        except (OSError, RuntimeError) as exc:
            logger.warning("skill reload after improve failed (%s)", type(exc).__name__)
    return control.answer(ImproveResult, result)


async def post_skill_evals_run(request: Request) -> JSONResponse:
    """POST .../skills/{skill_name}/evals/run — run the golden suite now (operator)."""
    control = _Control(request, "skill.evals.run")
    pre = await control.preflight()
    if isinstance(pre, JSONResponse):
        return pre
    result = await control.call(pre[0], "run_evals")
    if isinstance(result, JSONResponse):
        return result
    return control.answer(EvalsResult, result)


async def post_skill_evals_regen(request: Request) -> JSONResponse:
    """POST .../skills/{skill_name}/evals/regen — regenerate machine anchors (operator)."""
    control = _Control(request, "skill.evals.regen")
    pre = await control.preflight()
    if isinstance(pre, JSONResponse):
        return pre
    agent, body = pre
    if not _confirmed(body):
        return control.refuse(
            "denied", "regen requires explicit confirmation: 'confirm': true", 400
        )
    result = await control.call(agent, "regen_evals")
    if isinstance(result, JSONResponse):
        return result
    return control.answer(EvalsResult, result)


__all__ = [
    "get_skill_improver",
    "post_skill_evals_regen",
    "post_skill_evals_run",
    "post_skill_improve",
]
