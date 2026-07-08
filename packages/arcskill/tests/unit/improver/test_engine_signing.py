"""SPEC-033 D3 — arcskill.improver mutations are signed on write via the injected Signer.

``apply_result`` calls the injected ``Signer`` seam to sign the mutated SKILL.md
so the hub re-verifies it on reload. Without a signer (personal, relaxable) no
sidecar is written. The seam is provider-free: arcskill.improver knows only the Protocol;
this test injects a real arctrust-backed sidecar signer to prove the bytes carry
a valid detached signature pinned to the agent key.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

from arcskill.improver.candidate_store import CandidateStore
from arcskill.improver.engine import SkillOptimizer
from arcskill.improver.models import Candidate
from arctrust import sign_artifact, verify_artifact
from arctrust.artifact import ArtifactSignature
from arctrust.identity import AgentIdentity

_SIDECAR = ".arcsig"


class _ArctrustSigner:
    """Agent-DID sidecar Signer (the shape arcagent injects), arctrust-backed."""

    def __init__(self, did: str, private_key: bytes) -> None:
        self._did = did
        self._key = private_key

    def sign(self, path: Path, content: bytes) -> None:
        manifest = sign_artifact(content, signer_did=self._did, private_key=self._key)
        path.with_name(path.name + _SIDECAR).write_text(manifest.to_json(), encoding="utf-8")


def _optimizer(workspace: Path, *, signer: _ArctrustSigner | None) -> SkillOptimizer:
    return SkillOptimizer(
        config=AsyncMock(),
        evaluator=AsyncMock(),
        reflector=AsyncMock(),
        guardrails=AsyncMock(),
        store=CandidateStore(workspace),
        signer=signer,
    )


def _candidate() -> Candidate:
    return Candidate(id="abc123", text="# Improved skill\nDo better.\n", generation=1)


def test_apply_result_signs_mutation(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skill_path = workspace / "SKILL.md"
    skill_path.write_text("# Seed\n", encoding="utf-8")
    ident = AgentIdentity.generate(org="arc", agent_type="exec")

    opt = _optimizer(workspace, signer=_ArctrustSigner(ident.did, ident.signing_seed))
    opt.apply_result(
        "test-skill",
        _candidate(),
        skill_path=skill_path,
        seed_scores={"accuracy": 3.0},
        trace_ids=["t1"],
    )
    sidecar = skill_path.with_name(skill_path.name + _SIDECAR)
    manifest = ArtifactSignature.from_json(sidecar.read_text(encoding="utf-8"))
    assert (
        verify_artifact(skill_path.read_bytes(), manifest, trusted_public_key=ident.public_key)
        is True
    )


def test_apply_result_without_signer_writes_no_sidecar(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skill_path = workspace / "SKILL.md"
    skill_path.write_text("# Seed\n", encoding="utf-8")

    opt = _optimizer(workspace, signer=None)
    opt.apply_result(
        "test-skill",
        _candidate(),
        skill_path=skill_path,
        seed_scores={"accuracy": 3.0},
        trace_ids=["t1"],
    )
    assert not skill_path.with_name(skill_path.name + _SIDECAR).exists()
