"""`/api/agents/{id}/skills/{skill_name}/*` — evals view + version timeline (COMP-010).

SPEC-054 REQ-120. Read routes are discovery-free, following ``knowledge.py``'s
facade/status conventions:

* The eval-case list resolves the skill's bundle through arcagent's capability
  inventory seam (``agent_skill_rows`` — no globbing in arcui, REQ-096) and
  classifies provenance through arcskill's ``load_suite`` (the AST walk lives
  in arcskill, never in an arcui route).
* The version timeline / candidate body / diff read the arcstore mirror via
  ``app.state.observe`` — metadata-only list payloads, 200-empty vs
  503-unreadable, tombstone rows for pruned bodies (``body_hash`` is None).
* The diff is computed server-side (``difflib.unified_diff``) and memoized per
  ``(hash_a, hash_b)`` content-hash pair.

Rollback is the one mutation: operator-gated, confirm-gated, and audited with
ONE ``ui.mutation`` event (from→to candidate ids) through the shared COMP-010
helper. It calls ``CandidateStore.rollback`` directly against the agent
workspace — the same direct-workspace mutation shape as ``files_write.py``
(arcui runs no improver); the flip is non-destructive (manifest
``active_candidate_id`` only) and atomic. A retired skill rejects rollback
with 409 — revive is a distinct, separately-gated operation.
"""

from __future__ import annotations

import difflib
from functools import lru_cache
from pathlib import Path
from typing import Any

from arcskill.improver.candidate_store import (  # type: ignore[import-untyped]  # reason: arcskill ships no py.typed marker
    CandidateStore,
)
from arcskill.improver.evalgate import (  # type: ignore[import-untyped]  # reason: arcskill ships no py.typed marker
    load_suite,
)
from starlette.requests import Request
from starlette.responses import JSONResponse

from arcui.audit import emit_mutation_audit
from arcui.routes.agent_detail._common import _agent_root, logger
from arcui.routes.agent_detail.capabilities import _live_agent, agent_skill_rows
from arcui.schemas import (
    ErrorResponse,
    SkillEvalCase,
    SkillEvalCasesResponse,
    SkillRollbackResponse,
    SkillVersionBodyResponse,
    SkillVersionDiffResponse,
    SkillVersionsResponse,
)

_ROLLBACK_WARNING = (
    "Rolled back to a prior candidate. Its stored scores are historical — "
    "measured when the candidate was produced, not re-validated by this flip."
)


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse(ErrorResponse(error=message).model_dump(mode="json"), status_code=status)


def _store_unreadable(exc: Exception) -> JSONResponse:
    """Surface a mirror/store failure verbatim, distinct from empty (200)."""
    logger.warning("skill version route: store unreadable: %s", exc)
    return _error(str(exc), 503)


async def _skill_dir(request: Request, agent_root: Path, skill_name: str) -> Path | None:
    """Resolve a skill's bundle dir via the inventory seam (last root wins)."""
    agent_id = request.path_params["id"]
    rows = await agent_skill_rows(agent_root, _live_agent(request, agent_id))
    matches = [r for r in rows if r.get("name") == skill_name and r.get("source_path")]
    if not matches:
        return None
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
                provenance="machine" if case.machine_authored else "human",
            )
            for case in cases
        ]
    )
    return JSONResponse(payload.model_dump(mode="json"))


async def get_skill_versions(request: Request) -> JSONResponse:
    """GET .../skills/{skill_name}/versions — metadata-only lineage timeline."""
    agent_id = request.path_params["id"]
    skill_name = request.path_params["skill_name"]
    if _agent_root(request, agent_id) is None:
        return _error("Agent not found", 404)
    limit = int(request.query_params.get("limit", "100"))
    try:
        rows = await request.app.state.observe.skill_versions(skill_name, limit=limit)
    except Exception as exc:  # reason: 503-unreadable vs 200-empty (REQ-097 pattern)
        return _store_unreadable(exc)
    payload = SkillVersionsResponse(items=[_version_item(r) for r in rows])
    return JSONResponse(payload.model_dump(mode="json"))


async def get_skill_version_body(request: Request) -> JSONResponse:
    """GET .../versions/{candidate_id}/body — full text; 404 for a tombstone."""
    agent_id = request.path_params["id"]
    skill_name = request.path_params["skill_name"]
    candidate_id = request.path_params["candidate_id"]
    if _agent_root(request, agent_id) is None:
        return _error("Agent not found", 404)
    try:
        body = await request.app.state.observe.skill_candidate_body(skill_name, candidate_id)
    except Exception as exc:  # reason: 503-unreadable vs 200-empty (REQ-097 pattern)
        return _store_unreadable(exc)
    if body is None:
        return _error(f"candidate {candidate_id!r} has no stored body (pending or pruned)", 404)
    payload = SkillVersionBodyResponse(candidate_id=candidate_id, body=body)
    return JSONResponse(payload.model_dump(mode="json"))


async def get_skill_version_diff(request: Request) -> JSONResponse:
    """GET .../versions/diff?a=&b= — server-side unified diff, memoized."""
    agent_id = request.path_params["id"]
    skill_name = request.path_params["skill_name"]
    if _agent_root(request, agent_id) is None:
        return _error("Agent not found", 404)
    a = request.query_params.get("a")
    b = request.query_params.get("b")
    if not a or not b:
        return _error("both 'a' and 'b' candidate ids are required", 400)
    observe = request.app.state.observe
    try:
        rows = await observe.skill_versions(skill_name)
        hash_for = {r["candidate_id"]: r.get("body_hash") for r in rows}
        sides: dict[str, str] = {}
        for cid in (a, b):
            if cid not in hash_for:
                return _error(f"candidate {cid!r} not found for skill {skill_name!r}", 404)
            body = await observe.skill_candidate_body(skill_name, cid)
            if hash_for[cid] is None or body is None:
                return _error(f"candidate {cid!r} has no stored body (pending or pruned)", 404)
            sides[cid] = body
    except Exception as exc:  # reason: 503-unreadable vs 200-empty (REQ-097 pattern)
        return _store_unreadable(exc)
    diff = _unified_diff(hash_for[a], hash_for[b], sides[a], sides[b])
    payload = SkillVersionDiffResponse(a=a, b=b, diff=diff)
    return JSONResponse(payload.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# Rollback (the one mutation)
# ---------------------------------------------------------------------------


async def post_skill_rollback(request: Request) -> JSONResponse:
    """POST .../skills/{skill_name}/rollback — confirm-gated, audited flip."""
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

    store = CandidateStore(agent_root / "workspace")
    target = f"skill://{agent_id}/{skill_name}"
    try:
        if store.lifecycle_state(skill_name) == "retired":
            return _error(
                f"skill {skill_name!r} is retired; revive it first — rollback does not revive",
                409,
            )
        from_id = store.load_manifest(skill_name).get("active_candidate_id")
        store.rollback(skill_name, candidate_id)
    except ValueError as exc:
        # CandidateStore raises ValueError for both an unknown candidate and a
        # path-unsafe name/id — map "not found" to 404, the rest to 400.
        message = str(exc)
        emit_mutation_audit(
            request,
            target=target,
            operation="skill.rollback",
            outcome="error",
            detail=message,
        )
        return _error(message, 404 if "not found" in message else 400)

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
    "post_skill_rollback",
]
