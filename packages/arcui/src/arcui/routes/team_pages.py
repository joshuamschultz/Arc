"""Fleet-level HTTP routes — aggregations across all agents.

Each handler walks the roster (single source of truth via
``request.app.state.roster_provider``) and aggregates per-agent files via the
gateway's read-only ``fs_reader`` chokepoint. arcui never opens an agent file
directly; this module is the structural enforcement of acceptance criterion 16.

Endpoint surface (SDD §6):

* GET /api/team/roster
* GET /api/team/policy/bullets
* GET /api/team/policy/stats
* GET /api/team/tasks
* GET /api/team/tools-skills
* GET /api/team/audit
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from arcgateway import fs_reader, policy_parser
from arcgateway.fs_reader import FileTooLargeError, PathTraversalError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.query_validators import safe_int
from arcui.routes.agent_detail.capabilities import agent_skill_rows
from arcui.routes.agent_detail.tools import _BUILTIN_CLASSIFICATION
from arcui.schemas import (
    AuditEventsResponse,
    PolicyBulletsResponse,
    TasksResponse,
    TeamPolicyStatsResponse,
    TeamToolsSkillsResponse,
)

logger = logging.getLogger(__name__)

_CALLER_DID = "did:arc:ui:viewer"

# Effect classification for the built-in tool surface, so the fleet matrix can
# show read-only / write / external without a per-agent scan. Agent- and
# module-supplied tools surface full classification on the agent Tools tab.
_BUILTIN_CLASS: dict[str, str] = dict(_BUILTIN_CLASSIFICATION)

# ---------------------------------------------------------------------------
# Roster
# ---------------------------------------------------------------------------


async def get_roster(request: Request) -> JSONResponse:
    """GET /api/team/roster — every agent on disk, with a live online overlay.

    ``online`` is the only connect-status signal: it comes straight from
    ``app.state.agent_registry`` (populated by the embedded gateway's
    ``embedded_agents.py`` when a chat-loaded agent is cached, or by a
    WebSocket agent connection), which is truthful for every deployment —
    unlike the arc-stack.sh-only ``agent-state.json`` file this route used
    to read for a ``degraded`` flag. Nothing writes that file under the
    embedded-gateway architecture, so ``degraded`` was permanently false in
    every real deployment; removed rather than kept as a dead placeholder.
    """
    roster = _roster(request)
    data = {"agents": [_roster_to_dict(r) for r in roster]}
    return JSONResponse(data)


def _roster(request: Request) -> list[Any]:
    provider = getattr(request.app.state, "roster_provider", None)
    return list(provider() if provider is not None else [])


def _roster_to_dict(entry: Any) -> dict[str, Any]:
    return {
        "agent_id": entry.agent_id,
        "name": entry.name,
        "did": entry.did,
        "org": entry.org,
        "type": entry.type,
        "workspace_path": entry.workspace_path,
        "model": entry.model,
        "provider": entry.provider,
        "online": entry.online,
        "display_name": entry.display_name,
        "color": entry.color,
        "role_label": entry.role_label,
        "hidden": entry.hidden,
    }


# ---------------------------------------------------------------------------
# Policy aggregation
# ---------------------------------------------------------------------------


async def get_policy_bullets(request: Request) -> JSONResponse:
    """GET /api/team/policy/bullets — every bullet across the fleet."""
    out: list[dict[str, Any]] = []
    for entry in _roster(request):
        bullets = _read_agent_policy(entry)
        for b in bullets:
            d = _bullet_to_dict(b)
            d["agent_id"] = entry.agent_id
            out.append(d)
    return JSONResponse(PolicyBulletsResponse(bullets=out).model_dump(mode="json"))


async def get_policy_stats(request: Request) -> JSONResponse:
    """GET /api/team/policy/stats — fleet aggregates + per-agent breakdown."""
    per_agent: list[dict[str, Any]] = []
    total = 0
    active = 0
    retired = 0
    score_sum = 0
    for entry in _roster(request):
        bullets = _read_agent_policy(entry)
        a_active = sum(1 for b in bullets if not b.retired)
        a_retired = sum(1 for b in bullets if b.retired)
        a_avg = sum(b.score for b in bullets if not b.retired) / a_active if a_active else 0.0
        per_agent.append(
            {
                "agent_id": entry.agent_id,
                "total": len(bullets),
                "active": a_active,
                "retired": a_retired,
                "avg_score": a_avg,
            }
        )
        total += len(bullets)
        active += a_active
        retired += a_retired
        score_sum += sum(b.score for b in bullets if not b.retired)

    return JSONResponse(
        TeamPolicyStatsResponse(
            total=total,
            active=active,
            retired=retired,
            avg_score=(score_sum / active) if active else 0.0,
            per_agent=per_agent,
        ).model_dump(mode="json")
    )


def _read_agent_policy(entry: Any) -> list[policy_parser.PolicyBullet]:
    workspace = Path(entry.workspace_path) / "workspace"
    try:
        content = fs_reader.read_file(
            scope="agent",
            agent_id=entry.agent_id,
            agent_root=workspace,
            rel_path="policy.md",
            caller_did=_CALLER_DID,
        )
    except (FileNotFoundError, PathTraversalError, FileTooLargeError):
        return []
    return policy_parser.parse_bullets(content.content)


def _bullet_to_dict(b: policy_parser.PolicyBullet) -> dict[str, Any]:
    return {
        "id": b.id,
        "text": b.text,
        "score": b.score,
        "uses": b.uses,
        "reviewed": b.reviewed.isoformat() if b.reviewed else None,
        "created": b.created.isoformat() if b.created else None,
        "source": b.source,
        "retired": b.retired,
    }


# ---------------------------------------------------------------------------
# Tasks aggregation
# ---------------------------------------------------------------------------


async def get_tasks(request: Request) -> JSONResponse:
    """GET /api/team/tasks — arcstore task rows, stamped with owning agent_id."""
    did_to_agent = {entry.did: entry.agent_id for entry in _roster(request)}
    rows = await request.app.state.observe.tasks()
    out: list[dict[str, Any]] = []
    for row in rows:
        row = dict(row)
        row["agent_id"] = did_to_agent.get(row.get("owner_did"))
        out.append(row)
    return JSONResponse(TasksResponse(tasks=out).model_dump(mode="json"))


# ---------------------------------------------------------------------------
# Tools & Skills aggregation
# ---------------------------------------------------------------------------


async def get_tools_skills(request: Request) -> JSONResponse:
    """GET /api/team/tools-skills — fleet skills directory + tools matrix."""
    skills: list[dict[str, Any]] = []
    tools_by_name: dict[str, dict[str, Any]] = {}
    registry = request.app.state.agent_registry

    for entry in _roster(request):
        # Skills via the arcagent inventory seam — same set the agent loads.
        for s in await _read_agent_skills(entry):
            s["agent_id"] = entry.agent_id
            skills.append(s)
        # Tools from live registration (agents not connected contribute none).
        live = registry.get(entry.agent_id)
        if live is None:
            continue
        for tool in live.registration.tools:
            existing = tools_by_name.setdefault(
                tool,
                {"name": tool, "agents": [], "classification": _BUILTIN_CLASS.get(tool, "")},
            )
            if entry.agent_id not in existing["agents"]:
                existing["agents"].append(entry.agent_id)

    return JSONResponse(
        TeamToolsSkillsResponse(
            skills=skills,
            tools=list(tools_by_name.values()),
        ).model_dump(mode="json")
    )


async def _read_agent_skills(entry: Any) -> list[dict[str, Any]]:
    # Reuse the agent-detail seam so the fleet list and the per-agent tab
    # surface the identical set the agent loads, each with source_root + status.
    return await agent_skill_rows(Path(entry.workspace_path))


# ---------------------------------------------------------------------------
# Audit aggregation
# ---------------------------------------------------------------------------


async def get_audit(request: Request) -> JSONResponse:
    """GET /api/team/audit — fleet audit chain (last N), newest first.

    ``target`` (optional) narrows to one audited resource — e.g.
    ``task:<id>`` for a task's activity timeline (SDD §6 FR-12).
    """
    limit, err = safe_int(
        request.query_params.get("limit"),
        default=100,
        min_=1,
        max_=1000,
        error_label="Invalid limit",
    )
    if err is not None:
        return err
    target = request.query_params.get("target")
    events = await request.app.state.observe.audit(limit=limit, target=target)
    return JSONResponse(AuditEventsResponse(events=events).model_dump(mode="json"))


routes = [
    Route("/api/team/roster", get_roster, methods=["GET"]),
    Route("/api/team/policy/bullets", get_policy_bullets, methods=["GET"]),
    Route("/api/team/policy/stats", get_policy_stats, methods=["GET"]),
    Route("/api/team/tasks", get_tasks, methods=["GET"]),
    Route("/api/team/tools-skills", get_tools_skills, methods=["GET"]),
    Route("/api/team/audit", get_audit, methods=["GET"]),
]
