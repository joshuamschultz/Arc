"""Document-repository index route — the verified OKF ``index.md`` per source (H-026).

A connected DOCUMENT/MAIL source (smb / dropbox / s3 / onedrive / gmail) keeps a
reserved OKF ``index.md`` under the agent workspace — "what's in this repo and
what's it for." This route surfaces exactly that one artifact, read-only, so an
operator can see a connected repository's contents and purpose from the Knowledge
area.

Four pillars, every one enforced here:

* **Operator-gated** — a viewer token gets 403; only the operator role may read a
  repository index (it is a projection of connected data).
* **Audited** — every read (allowed OR denied) emits one ``ui.mutation`` audit
  event through the shared COMP-010 helper, so the access is on the tamper-evident
  chain the Security screen ingests.
* **Authorized + scope-gated** — the read goes through
  ``MemoryOperator.read_collection_index``, bound to exactly one agent workspace and
  one source-id folder. It never reaches another agent, another source, or the
  remote origin; an ungranted/unknown source is an empty ``present=False`` result.
* **Fail-closed** — the operator returns the index body ONLY when arcokf verified it
  against every listed document. A tampered/stale/corrupt index degrades to
  ``verified=False`` with an empty body; this route renders it as-is and never
  reconstructs the unverified artifact.

Like ``knowledge.py``, this module owns no store logic and runs no SQL — it consumes
only the public ``arcmemory.operator.MemoryOperator`` seam.
"""

from __future__ import annotations

import logging
from pathlib import Path

from arcmemory.operator import MemoryOperator
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.audit import emit_mutation_audit
from arcui.routes.knowledge import _operator_for, _resolve_agent
from arcui.schemas import ErrorResponse

logger = logging.getLogger(__name__)

_OPERATION = "knowledge.doc_repo_index.read"


def _require_operator(request: Request) -> JSONResponse | None:
    """403 unless the authenticated role is operator. ``None`` means proceed."""
    if getattr(request.state, "role", None) != "operator":
        return JSONResponse(
            ErrorResponse(error="Operator role required").model_dump(mode="json"),
            status_code=403,
        )
    return None


async def get_source_index(request: Request) -> JSONResponse:
    """GET .../knowledge/sources/{source_id}/index — one source's verified OKF index.

    Operator-only. Reads the workspace-hosted ``index.md`` through the arcmemory
    facade, which verifies it fail-closed; the response mirrors the facade's honest
    ``present``/``verified`` state so the panel can distinguish "no index yet" from
    "index present but could not be verified" without guessing at payload shape.
    """
    agent_id = request.path_params["agent_id"]
    source_id = request.path_params["source_id"]

    denied = _require_operator(request)
    if denied is not None:
        emit_mutation_audit(
            request,
            target=f"agent:{agent_id}/source:{source_id}",
            operation=_OPERATION,
            outcome="denied",
        )
        return denied

    agent = _resolve_agent(request, agent_id)
    if agent is None:
        emit_mutation_audit(
            request,
            target=f"agent:{agent_id}/source:{source_id}",
            operation=_OPERATION,
            outcome="not_found",
        )
        return JSONResponse(
            ErrorResponse(error=f"agent {agent_id!r} not found").model_dump(mode="json"),
            status_code=404,
        )

    op: MemoryOperator = _operator_for(Path(agent.workspace_path), agent.did)
    try:
        view = op.read_collection_index(source_id)
    except Exception as exc:  # a genuine store/filesystem failure — surface, don't fake
        logger.warning("doc-repo index: store unreadable for %s/%s: %s", agent_id, source_id, exc)
        emit_mutation_audit(
            request,
            target=f"agent:{agent_id}/source:{source_id}",
            operation=_OPERATION,
            outcome="error",
        )
        return JSONResponse(
            ErrorResponse(error=str(exc)).model_dump(mode="json"), status_code=503
        )

    emit_mutation_audit(
        request,
        target=f"agent:{agent_id}/source:{source_id}",
        operation=_OPERATION,
        outcome="applied",
        detail=f"present={view.present},verified={view.verified},docs={view.document_count}",
    )
    return JSONResponse(view.model_dump(mode="json"))


routes = [
    Route(
        "/api/agents/{agent_id}/knowledge/sources/{source_id}/index",
        get_source_index,
        methods=["GET"],
    ),
]

__all__ = ["get_source_index", "routes"]
