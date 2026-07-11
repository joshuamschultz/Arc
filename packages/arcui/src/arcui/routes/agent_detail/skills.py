"""`/api/agents/{id}/skills` — the agent's skills as the runtime loads them.

Thin wrapper over the arcagent capability inventory seam (COMP-007/008): the
skill list is exactly what the loader discovers across its scan roots, each row
carrying ``source_root`` + the verbatim load ``status``. arcui does no skill
globbing of its own (REQ-096) — discovery lives in arcagent.
"""

from __future__ import annotations

from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse

from arcui.routes.agent_detail._common import (
    _agent_root,
    _compute_write_target,
    _read_text_or_empty,
)
from arcui.routes.agent_detail.capabilities import _live_agent, agent_skill_rows
from arcui.schemas import ErrorResponse, SkillDetailResponse, SkillsResponse

# source_root prefixes the loader hands out for agent-writable scan roots
# ("agent", "agent-skills", "workspace", "workspace-skills"). Builtins/global
# ("builtins*", "global*") never match — those bundles are read-only here.
_EDITABLE_SOURCE_PREFIXES = ("agent", "workspace")


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse(ErrorResponse(error=message).model_dump(mode="json"), status_code=status)


async def get_skills(request: Request) -> JSONResponse:
    agent_id = request.path_params["id"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return _error("Agent not found", 404)
    rows = await agent_skill_rows(agent_root, _live_agent(request, agent_id))
    return JSONResponse(SkillsResponse(skills=rows).model_dump(mode="json"))


async def get_skill_detail(request: Request) -> JSONResponse:
    """GET .../skills/{skill_name}/detail — SKILL.md body + edit target (U5)."""
    agent_id = request.path_params["id"]
    skill_name = request.path_params["skill_name"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return _error("Agent not found", 404)

    rows = await agent_skill_rows(agent_root, _live_agent(request, agent_id))
    matches = [r for r in rows if r.get("name") == skill_name]
    if not matches:
        return _error(f"skill {skill_name!r} not found", 404)
    row = matches[-1]  # last root wins, mirroring skill_versions._skill_dir

    source_path_str = str(row.get("source_path") or "")
    skill_md = Path(source_path_str) if source_path_str else None
    content = _read_text_or_empty(skill_md) if skill_md is not None else ""

    source_root = str(row.get("source_root") or "")
    editable = source_root.startswith(_EDITABLE_SOURCE_PREFIXES)
    write_root: str | None = None
    write_path: str | None = None
    if editable and skill_md is not None:
        write_root, write_path = _compute_write_target(agent_root, skill_md)
        editable = write_root is not None

    payload = SkillDetailResponse(
        name=str(row.get("name") or skill_name),
        version=str(row.get("version") or ""),
        description=str(row.get("description") or ""),
        source_root=source_root,
        source_path=source_path_str,
        status=str(row.get("status") or ""),
        status_detail=str(row.get("status_detail") or ""),
        content=content,
        editable=editable,
        write_root=write_root,
        write_path=write_path,
    )
    return JSONResponse(payload.model_dump(mode="json"))
