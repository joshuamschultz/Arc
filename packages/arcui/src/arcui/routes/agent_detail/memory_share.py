"""Operator share + decision history of one memory card (alpha-2 item 16).

* ``POST /api/agents/{id}/knowledge/{kind}/{item_id}/share`` — operator only, body
  ``{}``. Asks the RUNNING agent (``app.state.embedded_agent_cache``, keyed by DID)
  to share the card on the operator's decision. The agent's own promotion path
  runs every gate: the federal tier lock, a demotion (a demoted card never
  re-promotes), the secret gate (no override), the size cap and the clearance
  rule. ``decided_by`` is the deployment operator DID derived from the operator
  signing key — never a value the browser sends.
* ``GET /api/agents/{id}/knowledge/{kind}/{item_id}/decisions`` — any
  authenticated role; the card's verified ledger rows, oldest first (decisions,
  fingerprints, deciders; never content).

Every share attempt — applied, denied or failed — emits one
``memory.promotion.operator_share`` record (target ``agent:<id>``, the card in
``detail``). No record carries memory content or an error message.
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

logger = logging.getLogger("arcui.routes.agent_detail.memory_share")

_AUDIT_OPERATION = "memory.promotion.operator_share"
_KINDS = frozenset({"insight", "procedure", "entity"})

#: The agent's share status → HTTP status. ``published`` is the only success;
#: ``outcome_unknown`` is accepted-but-uncertain (never retried automatically).
_STATUS_CODES: dict[str, int] = {
    "published": 200,
    "outcome_unknown": 202,
    "tier_forbidden": 403,
    "clearance_refused": 403,
    "not_found": 404,
    "demoted": 409,
    "refused": 409,
    "disabled": 409,
    "blocked_secret": 422,
    "too_large": 422,
    "publisher_unavailable": 503,
}


def _running_agent(request: Request, agent_id: str) -> Any | None:
    cache = getattr(request.app.state, "embedded_agent_cache", None)
    did = _agent_did(request, agent_id)
    return cache.get(did) if cache is not None and did is not None else None


def _card(request: Request) -> tuple[str, str]:
    return str(request.path_params["kind"]), str(request.path_params["item_id"])


def _operator_did(request: Request) -> str:
    """The deployment operator DID — derived from the operator key, never from input."""
    from arctrust.policy import OperatorApprovalAuthority

    from arcui.routes.trust import operator_signer_for_request

    return OperatorApprovalAuthority(operator_signer_for_request(request)).did


async def share_memory_card(request: Request) -> JSONResponse:
    """POST — share one card on the operator's decision. Operator only; audited."""
    agent_id = request.path_params["id"]
    kind, item_id = _card(request)
    audit = _Audit(request, agent_id, kind, item_id)
    refusal = await _share_refusal(request, agent_id, kind)
    if refusal is not None:
        audit.record("denied", {"reason": refusal[0]})
        return _error(refusal[0], refusal[1])
    share = getattr(_running_agent(request, agent_id), "share_memory_item", None)
    if share is None:
        audit.record("failed", {"reason": "agent is not running in this process"})
        return _error("agent is not running in this process", 503)
    try:
        decided_by = _operator_did(request)
    except Exception as exc:  # reason: no operator custody -> no decider to record
        audit.record("failed", {"error": type(exc).__name__})
        return _error("operator signing authority is unavailable", 503)
    return await _share(share, audit, kind, item_id, decided_by)


async def _share_refusal(request: Request, agent_id: str, kind: str) -> tuple[str, int] | None:
    """Role, agent, kind and body checks; the refusal message and status, or ``None``."""
    if getattr(request.state, "role", None) != "operator":
        return "Operator role required", 403
    if _agent_root(request, agent_id) is None:
        return "Agent not found", 404
    if kind not in _KINDS:
        return f"kind must be one of: {', '.join(sorted(_KINDS))}", 422
    raw = await request.body()
    if not raw.strip():
        return None
    try:
        body = await read_json_object(request)
    except BodyTooLargeError:
        return "Request body too large", 413
    if body is None:
        return "Body must be a JSON object", 400
    if body:
        return f"unknown field(s): {', '.join(sorted(body))}", 422
    return None


async def _share(
    share: Any, audit: _Audit, kind: str, item_id: str, decided_by: str
) -> JSONResponse:
    try:
        result: Mapping[str, object] = await share(kind, item_id, decided_by=decided_by)
    except arcagent.CapabilityUnavailableError:
        audit.record("failed", {"reason": "memory promotion unavailable"})
        return _error("this agent's memory cannot share cards", 503)
    except ValueError:
        audit.record("denied", {"reason": "malformed card reference"})
        return _error("malformed card reference", 422)
    except Exception as exc:  # reason: never leak a message that may carry content or paths
        logger.warning("operator share failed (%s)", type(exc).__name__)
        audit.record("failed", {"error": type(exc).__name__})
        return _error("share failed", 500)
    status = str(result.get("status", ""))
    shared_ref = result.get("shared_ref")
    wire = {"status": status, "shared_ref": shared_ref if isinstance(shared_ref, str) else None}
    code = _STATUS_CODES.get(status, 500)
    audit.record("applied" if code < 300 else "denied", {**wire, "decided_by": decided_by})
    return JSONResponse(wire, status_code=code)


async def get_memory_card_decisions(request: Request) -> JSONResponse:
    """GET — the card's verified promotion decisions, oldest first (no content)."""
    agent_id = request.path_params["id"]
    kind, item_id = _card(request)
    if _agent_root(request, agent_id) is None:
        return _error("Agent not found", 404)
    if kind not in _KINDS:
        return _error(f"kind must be one of: {', '.join(sorted(_KINDS))}", 422)
    history = getattr(_running_agent(request, agent_id), "memory_decision_history", None)
    if history is None:
        return _error("agent is not running in this process", 503)
    try:
        rows = await history(kind, item_id)
    except arcagent.CapabilityUnavailableError:
        return _error("this agent's memory has no promotion ledger", 503)
    except ValueError:
        return _error("malformed card reference", 422)
    return JSONResponse({"kind": kind, "item_id": item_id, "decisions": list(rows)})


class _Audit:
    """One ``memory.promotion.operator_share`` record per attempt; the card in detail."""

    def __init__(self, request: Request, agent_id: str, kind: str, item_id: str) -> None:
        self._request = request
        self._target = f"agent:{agent_id}"
        self._card = {"kind": kind, "item_id": item_id}

    def record(self, outcome: str, extra: Mapping[str, object]) -> None:
        emit_mutation_audit(
            self._request,
            target=self._target,
            operation=_AUDIT_OPERATION,
            outcome=outcome,
            detail=json.dumps({**self._card, **extra}, sort_keys=True),
        )


__all__ = ["get_memory_card_decisions", "share_memory_card"]
