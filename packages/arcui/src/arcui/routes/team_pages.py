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

import json
import logging
import re
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arcgateway import fs_reader, policy_parser
from arcgateway.fs_reader import FileTooLargeError, PathTraversalError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

logger = logging.getLogger(__name__)

_CALLER_DID = "did:arc:ui:viewer"
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# Default path for the per-agent connect-state file written by arc-stack.sh.
_AGENT_STATE_FILE = Path.home() / ".arcagent" / "agent-state.json"

# SPEC-025 §TD-5 — reject state files older than this many seconds.
# Roughly one demo session window: anything older almost certainly belongs
# to a previous arc-stack run that was not cleaned up on shutdown.
_AGENT_STATE_MAX_AGE_SECONDS = 600.0


def _load_agent_state(
    state_file: Path | None = None,
    *,
    now: float | None = None,
    max_age_seconds: float = _AGENT_STATE_MAX_AGE_SECONDS,
) -> dict[str, str]:
    """Load the arc-stack per-agent state file (best-effort).

    Returns a dict mapping agent name → "connected" | "degraded".
    Missing file, parse error, or stale `_meta.written_at` returns an
    empty dict, which causes all roster entries to fall through to their
    existing ``online`` logic with ``degraded=False`` — identical to
    pre-SPEC-025 behaviour.
    """
    path = state_file if state_file is not None else _AGENT_STATE_FILE
    try:
        raw = path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    if not _state_is_fresh(parsed, now=now, max_age_seconds=max_age_seconds):
        return {}
    return {
        k: v
        for k, v in parsed.items()
        if k != "_meta" and isinstance(k, str) and isinstance(v, str)
    }


def _state_is_fresh(
    parsed: dict[str, Any],
    *,
    now: float | None,
    max_age_seconds: float,
) -> bool:
    """Return True iff the state file's ``_meta.written_at`` is recent enough.

    Files written by older arc-stack revisions (no _meta) are accepted —
    backward-compatible — so an in-place upgrade does not blank the roster.
    """
    meta = parsed.get("_meta")
    if not isinstance(meta, dict):
        return True  # legacy file with no _meta — accept
    written_at = meta.get("written_at")
    if not isinstance(written_at, str):
        return True
    try:
        ts = datetime.fromisoformat(written_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    current = now if now is not None else datetime.now(UTC).timestamp()
    age = current - ts.timestamp()
    return age <= max_age_seconds


# ---------------------------------------------------------------------------
# Roster
# ---------------------------------------------------------------------------


async def get_roster(request: Request) -> JSONResponse:
    """GET /api/team/roster — every agent on disk, with online overlay."""
    roster = _roster(request)
    state_file: Path | None = getattr(request.app.state, "agent_state_file", None)
    raw_state = _load_agent_state(state_file)
    # Defense-in-depth (SPEC-025 §M2): only honour state-file entries that
    # match an agent_id from the live roster. Drops anything injected by an
    # out-of-band writer that doesn't correspond to a real agent on disk.
    known_ids = {entry.agent_id for entry in roster}
    agent_state = {k: v for k, v in raw_state.items() if k in known_ids}
    return JSONResponse(
        {
            "agents": [_roster_to_dict(r, agent_state) for r in roster],
        }
    )


def _roster(request: Request) -> list[Any]:
    provider = getattr(request.app.state, "roster_provider", None)
    return list(provider() if provider is not None else [])


def _roster_to_dict(entry: Any, agent_state: dict[str, str]) -> dict[str, Any]:
    # degraded=True when arc-stack.sh recorded this agent as "degraded"
    # (it was attempted but failed to connect). An agent with no entry in
    # the state file is not degraded — it simply was never attempted on
    # this host (or the state file is absent, meaning we have no data).
    state = agent_state.get(entry.agent_id)
    degraded = state == "degraded"
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
        "degraded": degraded,
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
    return JSONResponse({"bullets": out})


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
        a_avg = (
            sum(b.score for b in bullets if not b.retired) / a_active
            if a_active
            else 0.0
        )
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
        {
            "total": total,
            "active": active,
            "retired": retired,
            "avg_score": (score_sum / active) if active else 0.0,
            "per_agent": per_agent,
        }
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
    """GET /api/team/tasks — every agent's tasks.json, stamped with agent_id."""
    out: list[dict[str, Any]] = []
    for entry in _roster(request):
        for task in _read_agent_json_array(entry, "tasks.json"):
            task = dict(task)
            task["agent_id"] = entry.agent_id
            out.append(task)
    return JSONResponse({"tasks": out})


def _read_agent_json_array(entry: Any, rel_path: str) -> list[dict[str, Any]]:
    workspace = Path(entry.workspace_path) / "workspace"
    try:
        content = fs_reader.read_file(
            scope="agent",
            agent_id=entry.agent_id,
            agent_root=workspace,
            rel_path=rel_path,
            caller_did=_CALLER_DID,
        )
    except (FileNotFoundError, PathTraversalError, FileTooLargeError):
        return []
    try:
        parsed = json.loads(content.content)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


# ---------------------------------------------------------------------------
# Tools & Skills aggregation
# ---------------------------------------------------------------------------


async def get_tools_skills(request: Request) -> JSONResponse:
    """GET /api/team/tools-skills — fleet skills directory + tools matrix."""
    skills: list[dict[str, Any]] = []
    tools_by_name: dict[str, dict[str, Any]] = {}
    registry = request.app.state.agent_registry

    for entry in _roster(request):
        # Skills via fs_reader (workspace/skills/*.md)
        for s in _read_agent_skills(entry):
            s["agent_id"] = entry.agent_id
            skills.append(s)
        # Tools from live registration (agents not connected contribute none).
        live = registry.get(entry.agent_id)
        if live is None:
            continue
        for tool in live.registration.tools:
            existing = tools_by_name.setdefault(
                tool, {"name": tool, "agents": []}
            )
            if entry.agent_id not in existing["agents"]:
                existing["agents"].append(entry.agent_id)

    return JSONResponse(
        {
            "skills": skills,
            "tools": list(tools_by_name.values()),
        }
    )


def _read_agent_skills(entry: Any) -> list[dict[str, Any]]:
    workspace = Path(entry.workspace_path) / "workspace"
    try:
        listing = fs_reader.list_tree(
            scope="agent",
            agent_id=entry.agent_id,
            agent_root=workspace,
            rel_path="skills",
            caller_did=_CALLER_DID,
            max_depth=1,
        )
    except PathTraversalError:
        return []

    skills: list[dict[str, Any]] = []
    for item in listing:
        if item.type != "file" or not item.path.endswith(".md"):
            continue
        try:
            content = fs_reader.read_file(
                scope="agent",
                agent_id=entry.agent_id,
                agent_root=workspace,
                rel_path=item.path,
                caller_did=_CALLER_DID,
            )
        except (FileNotFoundError, PathTraversalError, FileTooLargeError):
            continue
        skills.append(_parse_skill_frontmatter(item.path, content.content))
    return skills


def _parse_skill_frontmatter(rel_path: str, text: str) -> dict[str, Any]:
    base = rel_path.rsplit("/", 1)[-1].removesuffix(".md")
    fm: dict[str, str] = {}
    match = _FRONTMATTER_RE.match(text)
    if match:
        for line in match.group(1).splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                fm[key.strip()] = value.strip()
    return {
        "name": fm.get("name", base),
        "description": fm.get("description", ""),
        "version": fm.get("version", ""),
        "path": rel_path,
    }


# ---------------------------------------------------------------------------
# Audit aggregation
# ---------------------------------------------------------------------------


async def get_audit(request: Request) -> JSONResponse:
    """GET /api/team/audit — fleet audit ring buffer (last N)."""
    buffer: deque[dict[str, Any]] = getattr(request.app.state, "audit_buffer", None) or deque()
    try:
        limit = max(1, min(1000, int(request.query_params.get("limit", "100"))))
    except ValueError:
        return JSONResponse({"error": "Invalid limit"}, status_code=400)
    events = list(buffer)[-limit:]
    return JSONResponse({"events": events})


routes = [
    Route("/api/team/roster", get_roster, methods=["GET"]),
    Route("/api/team/policy/bullets", get_policy_bullets, methods=["GET"]),
    Route("/api/team/policy/stats", get_policy_stats, methods=["GET"]),
    Route("/api/team/tasks", get_tasks, methods=["GET"]),
    Route("/api/team/tools-skills", get_tools_skills, methods=["GET"]),
    Route("/api/team/audit", get_audit, methods=["GET"]),
]
