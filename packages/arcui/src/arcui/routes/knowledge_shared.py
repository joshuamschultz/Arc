"""Fleet shared knowledge — ``/api/team/knowledge/shared/*`` (H-027, alpha-2 item 16).

The signed, operator-scoped counterpart to the per-agent knowledge routes: a
window onto what agents have PROMOTED into the fleet collection under
``<team_root>/shared/knowledge``. Every read goes through
``FleetSharedKnowledgeService`` so it is access-scoped, classification-filtered
(no-read-up against the operator clearance), revocation-aware, and
signature/TOFU-verified exactly as an agent's own retrieval would be — arcui
adds no discovery logic of its own.

Reads (any authenticated role):

* ``GET /api/team/knowledge/shared[?kind=insight|procedure|entity][&include_demoted=1]``
  — typed listing: kind, every contributor (DID + roster display name), the
  promotion time, per-kind counts and, for a demoted document, its demotion.
* ``GET /api/team/knowledge/shared/search?q=`` — classification-filtered search.
* ``GET /api/team/knowledge/shared/{id}`` — one document with its signed
  provenance (who promoted which bytes, when, on what decision).

One mutation (operator only, audited ``knowledge.demote``):

* ``POST /api/team/knowledge/shared/{id}/demote`` ``{"reason": str}`` — the
  deployment operator key signs a tombstone; the bytes are retired, never
  erased, and every contributing agent's next sweep makes the demote its sticky
  decision, so the card never re-promotes.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.audit import emit_mutation_audit
from arcui.routes.agent_detail.config_files import BodyTooLargeError, read_json_object
from arcui.schemas import ErrorResponse

logger = logging.getLogger(__name__)

#: The dashboard observes at its own clearance; UNCLASSIFIED is the safe default
#: (no-read-up hides anything above it rather than laundering it into the view).
_DEFAULT_OPERATOR_CLEARANCE = "UNCLASSIFIED"
_OPERATOR_DID = "did:arc:operator"
_KINDS = ("insight", "procedure", "entity")
_DEMOTE_AUDIT = "knowledge.demote"
_MAX_REASON_CHARS = 500


class _OperatorAccess:
    """The dashboard's read context — a fixed operator DID at a bounded clearance."""

    def __init__(self, clearance: str) -> None:
        self.caller_did = _OPERATOR_DID
        self.clearance = clearance


def _team_root(request: Request) -> Path | None:
    root = getattr(request.app.state, "team_root", None)
    return Path(root) if root is not None else None


def _clearance(request: Request) -> str:
    return str(getattr(request.app.state, "operator_clearance", _DEFAULT_OPERATOR_CLEARANCE))


def _access(request: Request) -> _OperatorAccess:
    return _OperatorAccess(_clearance(request))


def _service(team_root: Path, operator_public_key: bytes | None = None) -> Any:
    from arcteam.shared_knowledge import FleetSharedKnowledgeService

    return FleetSharedKnowledgeService.for_team_root(
        team_root, operator_public_key=operator_public_key
    )


def _operator_signer(request: Request) -> Any:
    from arcui.routes.trust import operator_signer_for_request

    return operator_signer_for_request(request)


def _operator_public_key(request: Request) -> bytes | None:
    """The deployment operator's verify key (the demotion trust anchor), or ``None``."""
    try:
        return bytes(_operator_signer(request).public_key)
    except Exception as exc:  # reason: no custody -> no anchor; demotions read as none
        logger.warning("shared knowledge: operator key unavailable (%s)", type(exc).__name__)
        return None


def _display_names(request: Request) -> dict[str, str]:
    """Map contributor DID → roster display name so promotions are attributed to an agent."""
    provider = getattr(request.app.state, "roster_provider", None)
    if provider is None:
        return {}
    display: dict[str, str] = {}
    for entry in provider():
        did = getattr(entry, "did", "")
        if did:
            display[did] = getattr(entry, "display_name", "") or getattr(entry, "name", did)
    return display


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse(ErrorResponse(error=message).model_dump(mode="json"), status_code=status)


def _unreadable(exc: Exception) -> JSONResponse:
    logger.warning("shared knowledge route: collection unreadable: %s", exc)
    return _error(f"shared knowledge unavailable: {exc}", 503)


def _demotion_wire(demotion: Any) -> dict[str, str] | None:
    if demotion is None:
        return None
    return {
        "demoted_by": demotion.demoted_by,
        "reason": demotion.reason,
        "demoted_at": demotion.demoted_at,
    }


def _contributors(dids: tuple[str, ...], display: dict[str, str]) -> list[dict[str, str]]:
    return [{"did": did, "display": display.get(did, did)} for did in dids]


def _summary_wire(summary: Any, display: dict[str, str]) -> dict[str, Any]:
    return {
        "identifier": summary.reference.identifier,
        "title": summary.title,
        "kind": summary.kind,
        "classification": summary.classification,
        "tags": list(summary.tags),
        "owner_did": summary.owner_did,
        "owner_display": display.get(summary.owner_did, summary.owner_did),
        "contributors": _contributors(summary.contributors, display),
        "promoted_at": summary.promoted_at,
        "excerpt": summary.excerpt,
        "demotion": _demotion_wire(summary.demotion),
    }


def _counts(summaries: list[Any]) -> dict[str, int]:
    live = [summary for summary in summaries if summary.demotion is None]
    by_kind = Counter(summary.kind for summary in live)
    counts = {"all": len(live), **{kind: by_kind.get(kind, 0) for kind in _KINDS}}
    counts["demoted"] = len(summaries) - len(live)
    return counts


async def list_shared_knowledge(request: Request) -> JSONResponse:
    """Every promoted document the dashboard may read — typed, attributed, timed."""
    team_root = _team_root(request)
    kind = request.query_params.get("kind", "").strip()
    if kind and kind not in _KINDS:
        return _error(f"kind must be one of: {', '.join(_KINDS)}", 422)
    if team_root is None:
        return JSONResponse({"documents": [], "counts": _counts([])})
    include_demoted = request.query_params.get("include_demoted", "") in {"1", "true"}
    anchor = _operator_public_key(request) if include_demoted else None
    try:
        summaries = await _service(team_root, anchor).list_documents(
            _access(request), include_demoted=include_demoted
        )
    except (OSError, ValueError, PermissionError) as exc:
        return _unreadable(exc)
    display = _display_names(request)
    documents = [
        _summary_wire(summary, display)
        for summary in summaries
        if not kind or summary.kind == kind
    ]
    return JSONResponse({"documents": documents, "counts": _counts(summaries)})


async def search_shared_knowledge(request: Request) -> JSONResponse:
    """Search promoted documents visible at the dashboard's clearance."""
    team_root = _team_root(request)
    if team_root is None:
        return JSONResponse({"hits": []})
    query = request.query_params.get("q", "").strip()
    if not query:
        return JSONResponse({"hits": []})
    try:
        hits = await _service(team_root).search(query, _access(request))
    except (OSError, ValueError, PermissionError) as exc:
        return _unreadable(exc)
    return JSONResponse(
        {
            "hits": [
                {
                    "identifier": hit.reference.identifier,
                    "title": hit.title,
                    "excerpt": hit.excerpt,
                }
                for hit in hits
            ]
        }
    )


async def get_shared_document(request: Request) -> JSONResponse:
    """One promoted document in full, with its signed provenance."""
    team_root = _team_root(request)
    if team_root is None:
        return _not_found()
    identifier = request.path_params["identifier"]
    service = _service(team_root)
    try:
        document = await service.read(identifier, _access(request))
        provenance = await service.provenance(identifier, _access(request))
    except FileNotFoundError:
        return _not_found()
    except PermissionError as exc:
        return _error(str(exc), 403)
    except (OSError, ValueError) as exc:
        return _unreadable(exc)
    display = _display_names(request)
    return JSONResponse(
        {
            "identifier": document.reference.identifier,
            "title": document.title,
            "kind": document.kind,
            "content": document.content,
            "classification": document.classification,
            "tags": list(document.tags),
            "contributors": _contributors(document.contributors, display),
            "promoted_at": min((record.promoted_at for record in provenance), default=None),
            "provenance": [_provenance_wire(record, display) for record in provenance],
        }
    )


def _provenance_wire(record: Any, display: dict[str, str]) -> dict[str, Any]:
    return {
        "contributor_did": record.contributor_did,
        "contributor_display": display.get(record.contributor_did, record.contributor_did),
        "source_ref": record.source_ref,
        "digest": record.digest,
        "kind": record.kind,
        "decision": record.decision,
        "confidence": record.confidence,
        "classifier_version": record.classifier_version,
        "decided_by": record.decided_by,
        "promoted_at": record.promoted_at,
    }


async def demote_shared_document(request: Request) -> JSONResponse:
    """POST — demote one shared document under the operator's signed tombstone.

    Operator only. Body ``{"reason": str}`` (1..500 chars, nothing else). Every
    attempt — applied, denied or failed — emits one ``knowledge.demote`` record.
    """
    identifier = str(request.path_params["identifier"])
    audit = _DemoteAudit(request, identifier)
    if getattr(request.state, "role", None) != "operator":
        return audit.refuse("operator role required", 403)
    team_root = _team_root(request)
    if team_root is None:
        return audit.refuse("shared knowledge document not found", 404)
    reason = await _demote_reason(request)
    if isinstance(reason, JSONResponse):
        audit.record("denied", {"reason": "invalid body"})
        return reason
    try:
        signer = _operator_signer(request)
    except Exception as exc:  # reason: no operator custody -> nothing can be signed
        audit.record("failed", {"error": type(exc).__name__})
        return _error("operator signing authority is unavailable", 503)
    return await _demote(audit, team_root, identifier, signer, reason, _clearance(request))


async def _demote(
    audit: _DemoteAudit, team_root: Path, identifier: str, signer: Any, reason: str, clearance: str
) -> JSONResponse:
    service = _service(team_root, bytes(signer.public_key))
    try:
        demotion = await service.demote(
            identifier, operator_signer=signer, reason=reason, clearance=clearance
        )
    except FileNotFoundError:
        return audit.refuse("shared knowledge document not found", 404)
    except PermissionError as exc:
        return audit.refuse(str(exc), 403)
    except ValueError as exc:
        return audit.refuse(str(exc), 422)
    except OSError as exc:
        audit.record("failed", {"error": type(exc).__name__})
        return _unreadable(exc)
    wire = {"identifier": demotion.identifier, **(_demotion_wire(demotion) or {})}
    audit.record("applied", wire)
    return JSONResponse(wire)


async def _demote_reason(request: Request) -> str | JSONResponse:
    """The body's ``reason``; a refusal response for anything but ``{"reason": str}``."""
    try:
        body = await read_json_object(request)
    except BodyTooLargeError:
        return _error("Request body too large", 413)
    if body is None:
        return _error("Body must be a JSON object", 400)
    extra = sorted(set(body) - {"reason"})
    if extra:
        return _error(f"unknown field(s): {', '.join(extra)}", 422)
    reason = body.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > _MAX_REASON_CHARS:
        return _error(f"reason must be 1..{_MAX_REASON_CHARS} characters", 422)
    return reason


class _DemoteAudit:
    """One ``knowledge.demote`` mutation record per attempt (never document content)."""

    def __init__(self, request: Request, identifier: str) -> None:
        self._request = request
        self._target = f"shared_knowledge:{identifier}"

    def record(self, outcome: str, extra: dict[str, Any]) -> None:
        emit_mutation_audit(
            self._request,
            target=self._target,
            operation=_DEMOTE_AUDIT,
            outcome=outcome,
            detail=json.dumps(extra, sort_keys=True),
        )

    def refuse(self, message: str, status: int) -> JSONResponse:
        self.record("denied", {"reason": message})
        return _error(message, status)


def _not_found() -> JSONResponse:
    return _error("shared knowledge document not found", 404)


routes = [
    Route("/api/team/knowledge/shared", list_shared_knowledge, methods=["GET"]),
    Route("/api/team/knowledge/shared/search", search_shared_knowledge, methods=["GET"]),
    Route("/api/team/knowledge/shared/{identifier}", get_shared_document, methods=["GET"]),
    Route(
        "/api/team/knowledge/shared/{identifier}/demote",
        demote_shared_document,
        methods=["POST"],
    ),
]
