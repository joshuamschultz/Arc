"""Home's "NEEDS YOU" aggregation (H-001).

``GET /api/home/needs`` — one read that answers "what needs the operator right
now" across every queue that already has its own operator surface:

- pending approvals (workflow-sign requests, connector/mapping approvals,
  scenario grants) — the same rows ``/api/approvals`` lists;
- gated capabilities (unsigned/refused tools and skills across the fleet) —
  the same rows ``/api/trust/gated`` lists;
- tasks awaiting operator review (``status == "review"``, SPEC-056's
  ``requires_review`` gate) — the same rows ``/api/team/tasks`` lists.

This route does not replace any of those surfaces or their stores — it reads
through the exact same seams (``ApprovalStore``, ``arcagent.list_gated``,
``Observe.tasks``) and only aggregates their pending counts plus a short
preview so Home can answer "what needs me" in one round trip instead of the
browser firing three sequential requests (SPEC-026's <2s Home budget). Each
queue read runs concurrently and degrades to an empty queue on its own
failure — a saturated pool on one plane must never blank out what the other
two already know, and must never be reported as "all caught up" either
(``total`` only counts what was actually read).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import arcagent
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.schemas import HomeNeedsQueue, HomeNeedsResponse

logger = logging.getLogger("arcui.routes.home")

#: How many rows of each queue ride along in the response. Home links out to
#: the owning page (Approvals / Pending capabilities / Tasks) for the rest —
#: this is a preview, not a second copy of those screens.
_PREVIEW_LIMIT = 5


async def _pending_approvals(request: Request) -> list[dict[str, Any]]:
    """Every pending row — the same set ``/api/approvals`` (GET) lists."""
    store = getattr(request.app.state, "approval_store", None)
    if store is None:
        return []
    try:
        pending = await store.list(status="pending")
    except Exception:  # reason: a saturated pool must degrade, not sink the panel
        logger.exception("home needs: approvals read failed")
        return []
    return [a.model_dump(mode="json") for a in pending]


async def _pending_capabilities(request: Request) -> list[dict[str, Any]]:
    """Gated (non-loaded) capabilities across every roster agent."""
    provider = getattr(request.app.state, "roster_provider", None)
    entries = provider() if provider is not None else []
    gated: list[dict[str, Any]] = []
    for entry in entries:
        agent_root = Path(entry.workspace_path)
        try:
            items = await arcagent.list_gated(
                agent_root,
                agent_id=entry.agent_id,
                agent_label=entry.display_name,
                include_loaded=False,
            )
        except Exception:  # reason: fleet resilience — one bad agent never sinks the list
            logger.warning("home needs: capability inventory failed for %s", entry.agent_id)
            continue
        gated.extend(item.model_dump(mode="json") for item in items)
    return gated


async def _tasks_in_review(request: Request) -> list[dict[str, Any]]:
    """Tasks parked in ``review`` — SPEC-056's opt-in ``requires_review`` gate."""
    observe = getattr(request.app.state, "observe", None)
    if observe is None:
        return []
    try:
        rows = await observe.tasks(status="review")
    except Exception:  # reason: a saturated pool must degrade, not sink the panel
        logger.exception("home needs: review-tasks read failed")
        return []
    return list(rows)


async def get_needs(request: Request) -> JSONResponse:
    """GET /api/home/needs — the operator's aggregated pending-action queue."""
    approvals, capabilities, review_tasks = await asyncio.gather(
        _pending_approvals(request),
        _pending_capabilities(request),
        _tasks_in_review(request),
    )
    body = HomeNeedsResponse(
        approvals=HomeNeedsQueue(count=len(approvals), items=approvals[:_PREVIEW_LIMIT]),
        capabilities=HomeNeedsQueue(count=len(capabilities), items=capabilities[:_PREVIEW_LIMIT]),
        review_tasks=HomeNeedsQueue(count=len(review_tasks), items=review_tasks[:_PREVIEW_LIMIT]),
        total=len(approvals) + len(capabilities) + len(review_tasks),
    )
    return JSONResponse(body.model_dump(mode="json"))


routes = [
    Route("/api/home/needs", get_needs, methods=["GET"]),
]

__all__ = ["get_needs", "routes"]
