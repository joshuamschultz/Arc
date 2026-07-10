"""`/api/agents/{id}/skills` — the agent's skills as the runtime loads them.

Thin wrapper over the arcagent capability inventory seam (COMP-007/008): the
skill list is exactly what the loader discovers across its scan roots, each row
carrying ``source_root`` + the verbatim load ``status``. arcui does no skill
globbing of its own (REQ-096) — discovery lives in arcagent.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse

from arcui.routes.agent_detail._common import _agent_root
from arcui.routes.agent_detail.capabilities import _live_agent, agent_skill_rows
from arcui.schemas import ErrorResponse, SkillsResponse


async def get_skills(request: Request) -> JSONResponse:
    agent_id = request.path_params["id"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return JSONResponse(
            ErrorResponse(error="Agent not found").model_dump(mode="json"),
            status_code=404,
        )
    rows = await agent_skill_rows(agent_root, _live_agent(request, agent_id))
    return JSONResponse(SkillsResponse(skills=rows).model_dump(mode="json"))
