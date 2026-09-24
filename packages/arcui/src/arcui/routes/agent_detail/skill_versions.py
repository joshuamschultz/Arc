"""`/api/agents/{id}/skills/{skill_name}/*` — evals view + version timeline (COMP-010).

SPEC-054 REQ-120. Read routes are discovery-free, following ``knowledge.py``'s
facade/status conventions:

* The eval-case list resolves the skill's bundle through arcagent's capability
  inventory seam (``agent_skill_rows`` — no globbing in arcui, REQ-096) and
  classifies provenance through arcskill's ``load_suite`` (the AST walk lives
  in arcskill, never in an arcui route).
* The version timeline / body / diff read only the current agent's externally
  anchored, signed revision chain.
* The diff is computed server-side (``difflib.unified_diff``) and memoized per
  ``(hash_a, hash_b)`` content-hash pair.

Rollback signs a new forward activation from a verified prior body, reloads
the live agent, and checks the provider's current bytes before reporting success.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
from functools import lru_cache
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from arcskill.improver.candidate_store import CandidateStore
from arcskill.improver.evalgate import load_suite
from arctrust.policy import OperatorApprovalAuthority
from starlette.requests import Request
from starlette.responses import JSONResponse

from arcui.audit import emit_mutation_audit
from arcui.routes.agent_detail._common import _agent_root, logger
from arcui.routes.agent_detail.capabilities import _live_agent, agent_skill_rows
from arcui.routes.agent_detail.skills import _revision_resolver
from arcui.routes.trust import operator_signer_for_request
from arcui.schemas import (
    ErrorResponse,
    SkillEvalCase,
    SkillEvalCasesResponse,
    SkillPromoteGoldenResponse,
    SkillRollbackResponse,
    SkillVersionBodyResponse,
    SkillVersionDiffResponse,
    SkillVersionsResponse,
)

_ROLLBACK_WARNING = (
    "Created a new signed activation from a prior revision. History remains intact."
)


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse(ErrorResponse(error=message).model_dump(mode="json"), status_code=status)


def _store_unreadable(exc: Exception) -> JSONResponse:
    """Surface a mirror/store failure verbatim, distinct from empty (200)."""
    logger.warning("skill version route: store unreadable: %s", exc)
    return _error(str(exc), 503)


async def _skill_dir(
    request: Request, agent_root: Path, skill_name: str
) -> Path | JSONResponse | None:
    """Resolve a skill's bundle dir via the inventory seam (last root wins)."""
    agent_id = request.path_params["id"]
    resolver = _revision_resolver(request, agent_id, agent_root)
    rows = await agent_skill_rows(agent_root, _live_agent(request, agent_id), resolver)
    matches = [r for r in rows if r.get("name") == skill_name and r.get("source_path")]
    if not matches:
        return None
    if matches[-1].get("status") == "unavailable":
        return _error("Active skill revision is unavailable", 503)
    return Path(matches[-1]["source_path"]).parent


def _version_item(row: dict[str, Any]) -> dict[str, Any]:
    """Map a mirror row to the UI timeline shape — metadata only, never a body."""
    body_hash = row.get("body_hash")
    return {
        "candidate_id": row.get("candidate_id"),
        "generation": row.get("generation"),
        "parent_id": row.get("parent_id"),
        "scores": row.get("scores") or {},
        "active": bool(row.get("active")),
        "body_hash": body_hash,
        "tombstone": body_hash is None,
        "ts": row.get("ts"),
    }


async def _anchored_versions(
    request: Request, agent_root: Path, skill_name: str
) -> tuple[Any, Path, list[tuple[str, int, str, bool]]] | JSONResponse:
    agent_id = request.path_params["id"]
    resolver = _revision_resolver(request, agent_id, agent_root)
    if resolver is None:
        return _error("External skill revision authority is unavailable", 503)
    rows = await agent_skill_rows(agent_root, _live_agent(request, agent_id), resolver)
    matches = [row for row in rows if row.get("name") == skill_name]
    if not matches:
        return _error("Skill not found", 404)
    source_root = str(matches[-1].get("source_root") or "")
    if source_root not in {"agent-skills", "workspace-skills"}:
        return _error("Skill source has no anchored revisions", 403)
    capabilities_root = (
        "workspace/capabilities" if source_root == "workspace-skills" else "capabilities"
    )
    folder = agent_root / capabilities_root / "skills" / skill_name
    try:
        return resolver, folder, await asyncio.to_thread(resolver.revision_history, folder)
    except (OSError, RuntimeError, ValueError):
        return _error("Skill revision history is unavailable", 503)


@lru_cache(maxsize=256)
def _unified_diff(hash_a: str, hash_b: str, body_a: str, body_b: str) -> str:
    """Unified diff memoized per content-hash pair (bodies are hash-determined)."""
    return "".join(
        difflib.unified_diff(
            body_a.splitlines(keepends=True),
            body_b.splitlines(keepends=True),
            fromfile=hash_a,
            tofile=hash_b,
        )
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def get_skill_evals(request: Request) -> JSONResponse:
    """GET .../skills/{skill_name}/evals — golden cases + provenance (read-only)."""
    agent_id = request.path_params["id"]
    skill_name = request.path_params["skill_name"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return _error("Agent not found", 404)
    skill_dir = await _skill_dir(request, agent_root, skill_name)
    if isinstance(skill_dir, JSONResponse):
        return skill_dir
    if skill_dir is None:
        return _error(f"skill {skill_name!r} not found", 404)
    try:
        cases = load_suite(skill_dir)
    except Exception as exc:  # reason: surface store failure as 503, never fail-open empty
        return _store_unreadable(exc)
    payload = SkillEvalCasesResponse(
        items=[
            SkillEvalCase(
                nodeid=case.id,
                provenance="curated"
                if case.curated
                else ("machine" if case.machine_authored else "human"),
                gate_type=case.gate_type,
            )
            for case in cases
        ]
    )
    return JSONResponse(payload.model_dump(mode="json"))


async def post_skill_promote_golden(request: Request) -> JSONResponse:
    """POST .../skills/{skill_name}/promote — the operator "promote to golden" surface.

    Wraps the SAME ``arcskill.improver.emit_golden_case`` operation the CLI drives
    (locked design §6): no duplicate emission logic here. Operator-gated + audited; a
    ``judge_rubric`` case without a pinned judge id + rubric sha256 is rejected (400).
    """
    from arcskill.improver import CuratedGoldenCase, emit_golden_case
    from arcskill.improver.goldencase import CurationError

    agent_id = request.path_params["id"]
    skill_name = request.path_params["skill_name"]
    if getattr(request.state, "role", None) != "operator":
        return _error("Operator role required", 403)
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return _error("Agent not found", 404)
    skill_dir = await _skill_dir(request, agent_root, skill_name)
    if isinstance(skill_dir, JSONResponse):
        return skill_dir
    if skill_dir is None:
        return _error(f"skill {skill_name!r} not found", 404)
    try:
        body = await request.json()
    except ValueError:
        return _error("Invalid JSON body", 400)
    if not isinstance(body, dict):
        return _error("body must be a curation spec object", 400)
    body.setdefault("skill_name", skill_name)
    target = f"skill://{agent_id}/{skill_name}"
    try:
        case = CuratedGoldenCase.model_validate(body)
        case.validate_pinned()
    except (CurationError, ValueError) as exc:
        emit_mutation_audit(
            request,
            target=target,
            operation="skill.golden.curated",
            outcome="denied",
            detail=type(exc).__name__,
        )
        return _error(str(exc), 400)
    anchored = await _anchored_versions(request, agent_root, skill_name)
    if isinstance(anchored, JSONResponse):
        return anchored
    resolver, folder, history = anchored
    live_agent = _live_agent(request, agent_id)
    if not history or live_agent is None:
        return _error("Active skill runtime is unavailable", 503)
    try:
        signer = await asyncio.to_thread(operator_signer_for_request, request)
        operator_did = OperatorApprovalAuthority(signer).did

        def stage_and_activate() -> str:
            current = resolver.read_bundle(folder)
            with TemporaryDirectory() as temp:
                stage = Path(temp)
                for relative, data in current.items():
                    if relative in {"manifest.json", "manifest.json.arcsig"}:
                        continue
                    target_path = stage / relative
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    target_path.write_bytes(data)
                emitted = emit_golden_case(stage, case)
                updates = {
                    path.relative_to(stage).as_posix(): path.read_bytes()
                    for path in (stage / "evals").rglob("*")
                    if path.is_file()
                    and not path.name.endswith(".arcsig")
                    and current.get(path.relative_to(stage).as_posix()) != path.read_bytes()
                }
                resolver.revise(
                    folder,
                    current["SKILL.md"],
                    expected_sha256=hashlib.sha256(current["SKILL.md"]).hexdigest(),
                    signer=signer,
                    operator_did=operator_did,
                    resource_updates=updates,
                )
                return emitted.nodeid

        nodeid = await asyncio.to_thread(stage_and_activate)
        await live_agent.reload_or_raise()
        active = resolver.resolve(folder, "agent-skills")
        if nodeid not in {item.id for item in load_suite(active.parent)}:
            raise RuntimeError("curated case did not reach the active skill")
    except (CurationError, ValueError) as exc:
        emit_mutation_audit(
            request,
            target=target,
            operation="skill.golden.curated",
            outcome="error",
            detail=str(exc),
        )
        return _error(str(exc), 400)
    except (OSError, RuntimeError) as exc:
        emit_mutation_audit(
            request,
            target=target,
            operation="skill.golden.curated",
            outcome="error",
            detail=type(exc).__name__,
        )
        return _error("Signed golden activation is unavailable", 503)
    emit_mutation_audit(
        request,
        target=target,
        operation="skill.golden.curated",
        outcome="emitted",
        detail=f"nodeid={nodeid} gate_type={case.gate_type}",
    )
    payload = SkillPromoteGoldenResponse(
        status="emitted",
        skill_name=skill_name,
        nodeid=nodeid,
        gate_type=case.gate_type,
    )
    return JSONResponse(payload.model_dump(mode="json"))


async def get_skill_versions(request: Request) -> JSONResponse:
    """GET .../skills/{skill_name}/versions — verified activation lineage."""
    agent_id = request.path_params["id"]
    skill_name = request.path_params["skill_name"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return _error("Agent not found", 404)
    try:
        limit = max(1, min(int(request.query_params.get("limit", "100")), 512))
    except ValueError:
        return _error("Invalid version limit", 400)
    result = await _anchored_versions(request, agent_root, skill_name)
    if isinstance(result, JSONResponse):
        return result
    _, _, history = result
    rows: list[dict[str, Any]] = [
        {
            "candidate_id": digest,
            "generation": version,
            "parent_id": history[index + 1][0] if index + 1 < len(history) else None,
            "scores": {},
            "active": active,
            "body_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "ts": None,
        }
        for index, (digest, version, body, active) in enumerate(history[:limit])
    ]
    payload = SkillVersionsResponse(items=[_version_item(row) for row in rows])
    return JSONResponse(payload.model_dump(mode="json"))


async def get_skill_version_body(request: Request) -> JSONResponse:
    """GET .../versions/{candidate_id}/body — full text; 404 for a tombstone."""
    agent_id = request.path_params["id"]
    skill_name = request.path_params["skill_name"]
    candidate_id = request.path_params["candidate_id"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return _error("Agent not found", 404)
    result = await _anchored_versions(request, agent_root, skill_name)
    if isinstance(result, JSONResponse):
        return result
    _, _, history = result
    body = next((text for digest, _, text, _ in history if digest == candidate_id), None)
    if body is None:
        return _error(f"candidate {candidate_id!r} has no stored body (pending or pruned)", 404)
    payload = SkillVersionBodyResponse(candidate_id=candidate_id, body=body)
    return JSONResponse(payload.model_dump(mode="json"))


async def get_skill_version_diff(request: Request) -> JSONResponse:
    """GET .../versions/diff?a=&b= — server-side unified diff, memoized."""
    agent_id = request.path_params["id"]
    skill_name = request.path_params["skill_name"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return _error("Agent not found", 404)
    a = request.query_params.get("a")
    b = request.query_params.get("b")
    if not a or not b:
        return _error("both 'a' and 'b' candidate ids are required", 400)
    result = await _anchored_versions(request, agent_root, skill_name)
    if isinstance(result, JSONResponse):
        return result
    _, _, history = result
    bodies = {digest: body for digest, _, body, _ in history}
    if a not in bodies or b not in bodies:
        return _error("Skill revision not found", 404)
    body_a, body_b = bodies[a], bodies[b]
    diff = _unified_diff(
        hashlib.sha256(body_a.encode("utf-8")).hexdigest(),
        hashlib.sha256(body_b.encode("utf-8")).hexdigest(),
        body_a,
        body_b,
    )
    payload = SkillVersionDiffResponse(a=a, b=b, diff=diff)
    return JSONResponse(payload.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# Rollback (the one mutation)
# ---------------------------------------------------------------------------


async def post_skill_rollback(request: Request) -> JSONResponse:
    """POST .../skills/{skill_name}/rollback — new signed activation."""
    agent_id = request.path_params["id"]
    skill_name = request.path_params["skill_name"]
    if getattr(request.state, "role", None) != "operator":
        return _error("Operator role required", 403)
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return _error("Agent not found", 404)

    try:
        body = await request.json()
    except ValueError:
        return _error("Invalid JSON body", 400)
    candidate_id = body.get("candidate_id") if isinstance(body, dict) else None
    if not isinstance(candidate_id, str) or not candidate_id:
        return _error("'candidate_id' (string) is required", 400)
    if body.get("confirm") is not True:
        return _error("rollback requires explicit confirmation: set 'confirm': true", 400)

    target = f"skill://{agent_id}/{skill_name}"
    result = await _anchored_versions(request, agent_root, skill_name)
    if isinstance(result, JSONResponse):
        emit_mutation_audit(request, target=target, operation="skill.rollback", outcome="denied")
        return result
    resolver, folder, history = result
    live_agent = _live_agent(request, agent_id)
    if not history or live_agent is None:
        emit_mutation_audit(request, target=target, operation="skill.rollback", outcome="denied")
        return _error("Active skill runtime is unavailable", 503)
    selected = next((text for digest, _, text, _ in history if digest == candidate_id), None)
    if selected is None:
        emit_mutation_audit(request, target=target, operation="skill.rollback", outcome="denied")
        return _error("Skill revision not found", 404)
    store = CandidateStore(agent_root / "workspace")
    try:
        if store.lifecycle_state(skill_name) == "retired":
            return _error(
                f"skill {skill_name!r} is retired; revive it first — rollback does not revive",
                409,
            )
        from_id = history[0][0]
        signer = await asyncio.to_thread(operator_signer_for_request, request)
        await asyncio.to_thread(
            resolver.activate_prior,
            folder,
            candidate_id,
            expected_sha256=hashlib.sha256(history[0][2].encode("utf-8")).hexdigest(),
            signer=signer,
            operator_did=OperatorApprovalAuthority(signer).did,
        )
        await live_agent.reload_or_raise()
        loaded = next((entry for entry in live_agent.skills if entry.name == skill_name), None)
        if loaded is None or loaded.read_current is None or loaded.read_current() != selected:
            raise RuntimeError("activated skill did not reach the runtime")
    except ValueError as exc:
        emit_mutation_audit(
            request,
            target=target,
            operation="skill.rollback",
            outcome="denied",
            detail=type(exc).__name__,
        )
        return _error("Skill activation was refused", 409)
    except (OSError, RuntimeError) as exc:
        emit_mutation_audit(
            request,
            target=target,
            operation="skill.rollback",
            outcome="error",
            detail=type(exc).__name__,
        )
        return _error("Skill activation is unavailable", 503)

    emit_mutation_audit(
        request,
        target=target,
        operation="skill.rollback",
        outcome="applied",
        detail=f"from={from_id} to={candidate_id}",
    )
    payload = SkillRollbackResponse(
        status="applied",
        skill_name=skill_name,
        from_candidate_id=from_id,
        to_candidate_id=candidate_id,
        warning=_ROLLBACK_WARNING,
    )
    return JSONResponse(payload.model_dump(mode="json"))


__all__ = [
    "get_skill_evals",
    "get_skill_version_body",
    "get_skill_version_diff",
    "get_skill_versions",
    "post_skill_promote_golden",
    "post_skill_rollback",
]
