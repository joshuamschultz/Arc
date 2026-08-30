"""Fleet shared-knowledge read view — ``/api/team/knowledge/shared/*`` (H-027).

The signed, operator-scoped counterpart to the per-agent knowledge routes: a
read-only window onto what agents have PROMOTED into the fleet collection under
``<team_root>/shared/knowledge``. Every read goes through
``FleetSharedKnowledgeService`` so it is access-scoped, classification-filtered
(no-read-up against the operator clearance), revocation-aware, and
signature/TOFU-verified exactly as an agent's own retrieval would be — arcui
adds no discovery logic of its own.

Each promoted document is attributed to its owner DID, and the listing resolves
that DID to the roster's display name so an operator sees WHAT each agent has
shared. Reads accept any authenticated role; there are no mutations here (an
agent revokes its own document through its tools, never the dashboard).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.schemas import ErrorResponse

logger = logging.getLogger(__name__)

#: The dashboard observes at its own clearance; UNCLASSIFIED is the safe default
#: (no-read-up hides anything above it rather than laundering it into the view).
_DEFAULT_OPERATOR_CLEARANCE = "UNCLASSIFIED"
_OPERATOR_DID = "did:arc:operator"


class _OperatorAccess:
    """The dashboard's read context — a fixed operator DID at a bounded clearance."""

    def __init__(self, clearance: str) -> None:
        self.caller_did = _OPERATOR_DID
        self.clearance = clearance


def _team_root(request: Request) -> Path | None:
    root = getattr(request.app.state, "team_root", None)
    return Path(root) if root is not None else None


def _access(request: Request) -> _OperatorAccess:
    clearance = getattr(request.app.state, "operator_clearance", _DEFAULT_OPERATOR_CLEARANCE)
    return _OperatorAccess(str(clearance))


def _service(team_root: Path) -> Any:
    from arcteam.shared_knowledge import FleetSharedKnowledgeService

    return FleetSharedKnowledgeService.for_team_root(team_root)


def _owner_display(request: Request) -> dict[str, str]:
    """Map owner DID → roster display name so promotions are attributed to an agent."""
    provider = getattr(request.app.state, "roster_provider", None)
    if provider is None:
        return {}
    display: dict[str, str] = {}
    for entry in provider():
        did = getattr(entry, "did", "")
        if did:
            display[did] = getattr(entry, "display_name", "") or getattr(entry, "name", did)
    return display


def _unreadable(exc: Exception) -> JSONResponse:
    logger.warning("shared knowledge route: collection unreadable: %s", exc)
    return JSONResponse(
        ErrorResponse(error=f"shared knowledge unavailable: {exc}").model_dump(mode="json"),
        status_code=503,
    )


async def list_shared_knowledge(request: Request) -> JSONResponse:
    """Every promoted document the dashboard may read, attributed to its owner agent."""
    team_root = _team_root(request)
    if team_root is None:
        return JSONResponse({"documents": []})
    display = _owner_display(request)
    try:
        summaries = await _service(team_root).list_documents(_access(request))
    except (OSError, ValueError, PermissionError) as exc:
        return _unreadable(exc)
    documents = [
        {
            "identifier": summary.reference.identifier,
            "title": summary.title,
            "classification": summary.classification,
            "tags": list(summary.tags),
            "owner_did": summary.owner_did,
            "owner_display": display.get(summary.owner_did, summary.owner_did),
            "excerpt": summary.excerpt,
        }
        for summary in summaries
    ]
    return JSONResponse({"documents": documents})


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
    """One promoted document in full — classification-checked and signature-verified."""
    team_root = _team_root(request)
    if team_root is None:
        return _not_found()
    identifier = request.path_params["identifier"]
    try:
        document = await _service(team_root).read(identifier, _access(request))
    except FileNotFoundError:
        return _not_found()
    except PermissionError as exc:
        return JSONResponse(ErrorResponse(error=str(exc)).model_dump(mode="json"), status_code=403)
    except (OSError, ValueError) as exc:
        return _unreadable(exc)
    return JSONResponse(
        {
            "identifier": document.reference.identifier,
            "title": document.title,
            "content": document.content,
            "classification": document.classification,
            "tags": list(document.tags),
        }
    )


def _not_found() -> JSONResponse:
    return JSONResponse(
        ErrorResponse(error="shared knowledge document not found").model_dump(mode="json"),
        status_code=404,
    )


routes = [
    Route("/api/team/knowledge/shared", list_shared_knowledge, methods=["GET"]),
    Route("/api/team/knowledge/shared/search", search_shared_knowledge, methods=["GET"]),
    Route("/api/team/knowledge/shared/{identifier}", get_shared_document, methods=["GET"]),
]
