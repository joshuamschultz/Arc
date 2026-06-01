"""Tool / code / spawn observability routes (SPEC-028 FR-4).

Read-only, pull-based — mirrors the existing trace/stats routes (SPEC-026
D-007). No push, no polling machinery: each endpoint is a synchronous
request→response read from ``app.state.observe`` (the arcstore mirror).

- ``GET /api/runs/{run_id}/timeline`` — merged tool/code/llm/run timeline.
- ``GET /api/spawn-tree?root=<did>`` — parent→child lineage tree.
- ``GET /api/stats/by-identity?window=<w>`` — per-identity LLM cost (parent vs child).
"""

from __future__ import annotations

import re

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.schemas import ErrorResponse

_VALID_WINDOWS = frozenset({"1h", "24h", "7d", "30d"})
# NIST SI-10: same safe charset as the trace/identity filters elsewhere.
_VALID_ID_RE = re.compile(r"^[a-zA-Z0-9._:/-]{1,128}$")


def _invalid(msg: str) -> JSONResponse:
    return JSONResponse(ErrorResponse(error=msg).model_dump(mode="json"), status_code=400)


async def get_run_timeline(request: Request) -> JSONResponse:
    """GET /api/runs/{run_id}/timeline — ordered tool/code/llm/run events for a run."""
    run_id = request.path_params["run_id"]
    if not _VALID_ID_RE.match(run_id):
        return _invalid("Invalid run_id format")
    timeline = await request.app.state.observe.timeline(run_id=run_id)
    return JSONResponse({"run_id": run_id, "timeline": timeline})


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
    Route("/api/runs/{run_id}/timeline", get_run_timeline),
    Route("/api/spawn-tree", get_spawn_tree),
    Route("/api/stats/by-identity", get_stats_by_identity),
]
