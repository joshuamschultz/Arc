"""Tool / code / spawn observability routes (SPEC-028 FR-4).

Read-only, pull-based — mirrors the existing trace/stats routes (SPEC-026
D-007). No push, no polling machinery: each endpoint is a synchronous
request→response read from ``app.state.observe`` (the arcstore mirror).

- ``GET /api/runs`` — list of real runs (one per request_id), newest first.
  Operator and viewer sessions only: it has no ``APP_ROUTES`` row.
- ``GET /api/runs/{run_id}/timeline`` — merged tool/code/llm/run timeline.
- ``GET /api/runs/{run_id}/audit`` — signed audit rows joined on ``causal.run_id``.
- ``GET /api/runs/{run_id}/recalls`` — memory recall-attribution events for a run.
- ``GET /api/spawn-tree?root=<did>`` — parent→child lineage tree.
- ``GET /api/stats/by-identity?window=<w>`` — per-identity LLM cost (parent vs child).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.acting import is_app
from arcui.routes.runs_intake import load_intake_row, run_read_refusal
from arcui.schemas import ErrorResponse

logger = logging.getLogger(__name__)


def _unavailable(what: str) -> JSONResponse:
    """A transient read failure degrades to 503, never an unhandled 500.

    These panels poll a shared connection pool; when it is briefly saturated a
    query can time out. A clean 503 lets the page show 'temporarily unavailable'
    and retry, instead of surfacing a raw 500 as 'Failed to fetch'.
    """
    return JSONResponse(
        ErrorResponse(error=f"{what} temporarily unavailable").model_dump(mode="json"),
        status_code=503,
    )


_VALID_WINDOWS = frozenset({"1h", "24h", "7d", "30d"})
# NIST SI-10: same safe charset as the trace/identity filters elsewhere.
_VALID_ID_RE = re.compile(r"^[a-zA-Z0-9._:/-]{1,128}$")


def _invalid(msg: str) -> JSONResponse:
    return JSONResponse(ErrorResponse(error=msg).model_dump(mode="json"), status_code=400)


async def get_runs(request: Request) -> JSONResponse:
    """GET /api/runs[?agent=<did>][?window=<w>] — real runs, newest first.

    ``window`` (H-004/H-006) scopes the fold to runs touched in the last
    window instead of the last ``scan`` raw rows — Home's "today" card wants
    this; the ArcRun page and agent-detail runs tab omit it for full history.
    """
    agent = request.query_params.get("agent")
    if agent is not None and not _VALID_ID_RE.match(agent):
        return _invalid("Invalid agent format")
    window = request.query_params.get("window")
    if window is not None and window not in _VALID_WINDOWS:
        return _invalid("Invalid window. Use 1h, 24h, 7d, or 30d.")
    try:
        runs = await request.app.state.observe.runs(agent=agent, window=window)
    except Exception:  # reason: a saturated pool must degrade, not 500 the panel
        logger.exception("runs read failed")
        return _unavailable("runs")
    return JSONResponse({"runs": runs})


async def get_run_timeline(request: Request) -> JSONResponse:
    """GET /api/runs/{run_id}/timeline — ordered tool/code/llm/run events for a run."""
    run_id = request.path_params["run_id"]
    if not _VALID_ID_RE.match(run_id):
        return _invalid("Invalid run_id format")
    timeline = await request.app.state.observe.timeline(run_id=run_id)
    denial = await _app_read_refusal(request, run_id, "run.timeline.read", timeline)
    if denial is not None:
        return denial
    return JSONResponse({"run_id": run_id, "timeline": timeline})


async def _app_read_refusal(
    request: Request,
    run_id: str,
    operation: str,
    timeline: list[dict[str, Any]] | None = None,
) -> JSONResponse | None:
    """None when the caller may read this run; else one audited 404 (REQ-598).

    Operator and viewer sessions read any run. An app needs ``runs:read`` on
    the run's agent: the intake row's owner when the run was started through
    ``POST /api/runs``, else the one agent its own trace names. A run with no
    resolvable agent and a run outside the app's scope get the same 404, so an
    app cannot probe which runs exist.
    """
    if not is_app(request):
        return None
    row = await load_intake_row(request, run_id)
    if row is not None:
        agent = row.owner_did
    else:
        if timeline is None:
            timeline = await request.app.state.observe.timeline(run_id=run_id)
        agent = _single_agent(timeline)
    return run_read_refusal(request, run_id, agent, operation=operation)


def _single_agent(timeline: list[dict[str, Any]]) -> str | None:
    """The one agent DID a trace's own rows name; None when none or several.

    An app is bound to agents, so a trace it may read must belong to exactly
    one: a trace naming no agent (unknown run) or more than one is not served
    to an app (fail closed). Operator sessions never reach this.
    """
    agents = {str(row["actor_did"]) for row in timeline if row.get("actor_did")}
    return next(iter(agents)) if len(agents) == 1 else None


async def get_run_audit(request: Request) -> JSONResponse:
    """GET /api/runs/{run_id}/audit — the audit rows whose causal chain names this run."""
    run_id = request.path_params["run_id"]
    if not _VALID_ID_RE.match(run_id):
        return _invalid("Invalid run_id format")
    denial = await _app_read_refusal(request, run_id, "run.audit.read")
    if denial is not None:
        return denial
    events = await request.app.state.observe.run_audit(run_id)
    return JSONResponse({"run_id": run_id, "events": events})


async def get_run_recalls(request: Request) -> JSONResponse:
    """GET /api/runs/{run_id}/recalls — recall cards attributed to this run."""
    run_id = request.path_params["run_id"]
    if not _VALID_ID_RE.match(run_id):
        return _invalid("Invalid run_id format")
    denial = await _app_read_refusal(request, run_id, "run.recalls.read")
    if denial is not None:
        return denial
    events = await request.app.state.observe.run_recalls(run_id)
    return JSONResponse({"events": events})


async def get_spawn_tree(request: Request) -> JSONResponse:
    """GET /api/spawn-tree?root=<did> — parent→child lineage tree."""
    root = request.query_params.get("root")
    if root is not None and not _VALID_ID_RE.match(root):
        return _invalid("Invalid root DID format")
    tree = await request.app.state.observe.spawn_tree(root_did=root)
    return JSONResponse({"tree": tree})


async def get_stats_by_identity(request: Request) -> JSONResponse:
    """GET /api/stats/by-identity?window=<w> — per-identity LLM cost separation."""
    window = request.query_params.get("window", "24h")
    if window not in _VALID_WINDOWS:
        return _invalid("Invalid window. Use 1h, 24h, 7d, or 30d.")
    return JSONResponse(await request.app.state.observe.llm_by_identity(window))


routes = [
    Route("/api/runs", get_runs, methods=["GET"]),
    Route("/api/runs/{run_id}/timeline", get_run_timeline),
    Route("/api/runs/{run_id}/audit", get_run_audit, methods=["GET"]),
    Route("/api/runs/{run_id}/recalls", get_run_recalls, methods=["GET"]),
    Route("/api/spawn-tree", get_spawn_tree),
    Route("/api/stats/by-identity", get_stats_by_identity),
]
