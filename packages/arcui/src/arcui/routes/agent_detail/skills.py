"""`/api/agents/{id}/skills` — the agent's skills as the runtime loads them.

Thin wrapper over the arcagent capability inventory seam (COMP-007/008): the
skill list is exactly what the loader discovers across its scan roots, each row
carrying ``source_root`` + the verbatim load ``status``. arcui does no skill
globbing of its own (REQ-096) — discovery lives in arcagent.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import arcagent
from arctrust.policy import OperatorApprovalAuthority
from starlette.requests import Request
from starlette.responses import JSONResponse

from arcui.audit import emit_mutation_audit
from arcui.routes.agent_detail._common import (
    _agent_did,
    _agent_root,
    _compute_write_target,
    _read_text_or_empty,
)
from arcui.routes.agent_detail.capabilities import _live_agent, agent_skill_rows
from arcui.routes.trust import operator_signer_for_request
from arcui.schemas import ErrorResponse, SkillDetailResponse, SkillsResponse

# source_root prefixes the loader hands out for agent-writable scan roots
# ("agent", "agent-skills", "workspace", "workspace-skills"). Builtins/global
# ("builtins*", "global*") never match — those bundles are read-only here.
_EDITABLE_SOURCE_PREFIXES = ("agent", "workspace")


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse(ErrorResponse(error=message).model_dump(mode="json"), status_code=status)


def _revision_resolver(
    request: Request, agent_id: str, agent_root: Path
) -> arcagent.AnchoredSkillRevisionResolver | None:
    factory = getattr(request.app.state, "skill_revision_anchor_factory", None)
    did = _agent_did(request, agent_id)
    if factory is None or did is None:
        return None
    return arcagent.AnchoredSkillRevisionResolver(
        agent_did=did, config_path=agent_root / "arcagent.toml", anchor_factory=factory
    )


async def get_skills(request: Request) -> JSONResponse:
    agent_id = request.path_params["id"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return _error("Agent not found", 404)
    resolver = _revision_resolver(request, agent_id, agent_root)
    rows = await agent_skill_rows(agent_root, _live_agent(request, agent_id), resolver)
    return JSONResponse(SkillsResponse(skills=rows).model_dump(mode="json"))


async def get_skill_detail(request: Request) -> JSONResponse:
    """GET .../skills/{skill_name}/detail — SKILL.md body + edit target (U5)."""
    agent_id = request.path_params["id"]
    skill_name = request.path_params["skill_name"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return _error("Agent not found", 404)

    resolver = _revision_resolver(request, agent_id, agent_root)
    rows = await agent_skill_rows(agent_root, _live_agent(request, agent_id), resolver)
    matches = [r for r in rows if r.get("name") == skill_name]
    if not matches:
        return _error(f"skill {skill_name!r} not found", 404)
    row = matches[-1]  # last root wins, mirroring skill_versions._skill_dir
    source_path_str = str(row.get("source_path") or "")
    skill_md = Path(source_path_str) if source_path_str else None
    content: str | None = None

    source_root = str(row.get("source_root") or "")
    editable = source_root.startswith(_EDITABLE_SOURCE_PREFIXES)
    write_root: str | None = None
    write_path: str | None = None
    if editable and skill_md is not None:
        capabilities_root = (
            "workspace/capabilities" if source_root.startswith("workspace") else "capabilities"
        )
        folder = agent_root / capabilities_root / "skills" / skill_name
        write_root, write_path = _compute_write_target(agent_root, folder / "SKILL.md")
        editable = write_root is not None
        if resolver is not None:
            try:
                if row.get("status") == "unavailable":
                    if getattr(request.state, "role", None) != "operator":
                        return _error("Active skill revision unavailable", 503)
                    content = resolver.preview_original(folder)
                else:
                    revision = resolver.resolve(folder, source_root)
                    verified = resolver.read_current(folder, revision)
                    if verified is None:
                        return _error("Active skill revision unavailable", 503)
                    content = verified
                    source_path_str = str(revision)
            except (OSError, RuntimeError, ValueError):
                return _error("Active skill revision unavailable", 503)
    if row.get("status") == "unavailable" and content is None:
        return _error("Active skill revision unavailable", 503)
    if content is None and skill_md is not None:
        content = _read_text_or_empty(skill_md)

    payload = SkillDetailResponse(
        name=str(row.get("name") or skill_name),
        version=str(row.get("version") or ""),
        description=str(row.get("description") or ""),
        source_root=source_root,
        source_path=source_path_str,
        status="enrollment_required"
        if row.get("status") == "unavailable"
        else str(row.get("status") or ""),
        status_detail=str(row.get("status_detail") or ""),
        content=content or "",
        sha256=hashlib.sha256((content or "").encode("utf-8")).hexdigest(),
        editable=editable,
        write_root=write_root,
        write_path=write_path,
    )
    return JSONResponse(payload.model_dump(mode="json"))


async def put_skill_revision(request: Request) -> JSONResponse:
    """Save a reviewed skill edit through the signed capability lifecycle."""
    agent_id = request.path_params["id"]
    skill_name = request.path_params["skill_name"]
    target = f"skill://{agent_id}/{skill_name}"
    if getattr(request.state, "role", None) != "operator":
        emit_mutation_audit(request, target=target, operation="skill.revise", outcome="denied")
        return _error("operator_role_required", 403)
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return _error("Agent not found", 404)
    try:
        body = await request.json()
    except (ValueError, TypeError):
        return _error("Invalid JSON body", 400)
    if (
        not isinstance(body, dict)
        or not isinstance(body.get("content"), str)
        or not isinstance(body.get("expected_sha256"), str)
    ):
        return _error("content and expected_sha256 are required", 400)
    resolver = _revision_resolver(request, agent_id, agent_root)
    rows = await agent_skill_rows(agent_root, _live_agent(request, agent_id), resolver)
    matches = [row for row in rows if row.get("name") == skill_name]
    if not matches:
        return _error("Skill not found", 404)
    source_root = str(matches[-1].get("source_root") or "")
    if source_root not in {"agent-skills", "workspace-skills"}:
        return _error("Skill source cannot be revised here", 403)
    capabilities_root = (
        "workspace/capabilities" if source_root == "workspace-skills" else "capabilities"
    )
    folder = agent_root / capabilities_root / "skills" / skill_name
    if resolver is None:
        return _error("skill revision authority is unavailable; configure an external anchor", 503)
    try:
        signer = await asyncio.to_thread(operator_signer_for_request, request)
        digest = await asyncio.to_thread(
            resolver.revise,
            folder,
            body["content"].encode("utf-8"),
            expected_sha256=body["expected_sha256"],
            signer=signer,
            operator_did=OperatorApprovalAuthority(signer).did,
        )
    except ValueError as exc:
        emit_mutation_audit(
            request,
            target=target,
            operation="skill.revise",
            outcome="denied",
            detail=type(exc).__name__,
        )
        return _error(str(exc), 409 if "changed since" in str(exc) else 422)
    except (OSError, RuntimeError) as exc:
        emit_mutation_audit(
            request,
            target=target,
            operation="skill.revise",
            outcome="error",
            detail=type(exc).__name__,
        )
        return _error("skill revision unavailable", 503)
    live_agent = _live_agent(request, agent_id)
    if live_agent is not None:
        await live_agent.reload_or_raise()
    emit_mutation_audit(request, target=target, operation="skill.revise", outcome="applied")
    return JSONResponse({"status": "active", "sha256": digest, "skill_name": skill_name})
