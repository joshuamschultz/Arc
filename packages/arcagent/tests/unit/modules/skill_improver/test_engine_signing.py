"""SPEC-033 D3 — skill-improver mutations are signed on write.

``apply_result`` signs the mutated SKILL.md with the agent's DID key when a
signer is configured, so the loader re-verifies it on reload identically to
``create_skill`` output. Without a signer (personal, relaxable) no sidecar is
written.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

from arctrust.identity import AgentIdentity

from arcagent.capabilities import artifact_signing
from arcagent.modules.skill_improver.candidate_store import CandidateStore
from arcagent.modules.skill_improver.engine import SkillOptimizer
from arcagent.modules.skill_improver.models import Candidate


def _optimizer(workspace: Path, *, identity: AgentIdentity | None) -> SkillOptimizer:
    # apply_result only touches config-free deps (store + signer), so the
    # evaluator/reflector/guardrails/config can be light stand-ins.
    return SkillOptimizer(
        config=AsyncMock(),
        evaluator=AsyncMock(),
        reflector=AsyncMock(),
        guardrails=AsyncMock(),
        store=CandidateStore(workspace),
        signer_did=identity.did if identity else None,
        signing_key=identity.signing_seed if identity else None,
    )


def _candidate() -> Candidate:
    return Candidate(id="abc123", text="# Improved skill\nDo better.\n", generation=1)


def test_apply_result_signs_mutation(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skill_path = workspace / "SKILL.md"
    skill_path.write_text("# Seed\n", encoding="utf-8")
    ident = AgentIdentity.generate(org="arc", agent_type="exec")

    opt = _optimizer(workspace, identity=ident)
    opt.apply_result(
        "test-skill",
        _candidate(),
        skill_path=skill_path,
        seed_scores={"accuracy": 3.0},
        trace_ids=["t1"],
    )
    # Mutated bytes carry a valid detached signature pinned to the agent key.
    assert artifact_signing.verify_file(
        skill_path, skill_path.read_bytes(), trusted_public_key=ident.public_key
    ) is True


def test_apply_result_without_signer_writes_no_sidecar(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skill_path = workspace / "SKILL.md"
    skill_path.write_text("# Seed\n", encoding="utf-8")

    opt = _optimizer(workspace, identity=None)
    opt.apply_result(
        "test-skill",
        _candidate(),
        skill_path=skill_path,
        seed_scores={"accuracy": 3.0},
        trace_ids=["t1"],
    )
    assert not artifact_signing.sidecar_path(skill_path).exists()
