"""SPEC-044 Phase 7 — audit authority split + reversibility (AC-6, REQ-050/052).

AC-6: an applied code mutation is signed two ways by two authorities — the bundle
sidecar by the AGENT DID (provenance), the WORM audit chain by the OPERATOR key
(who-did-what). The keys are distinct and each attestation verifies ONLY under its own
authority. Plus: rollback cools off + emits an operator audit; the eval/patch paths
never touch operator/.audit locations.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arcskill.improver import ArcSkillImprover, ImproverConfig
from arcskill.improver.codepatch import apply_bundle_patch
from arcskill.improver.models import BundlePatch, BundleView, Candidate, EvalCase, EvalOutcome
from arctrust import OperatorKey, WormSink, sign_artifact, verify_chain
from arctrust.artifact import ArtifactSignature
from arctrust.identity import AgentIdentity

_BUGGY = b"def add(a, b):\n    return a - b\n"
_FIXED = b"def add(a, b):\n    return a + b\n"


class _FixMutator:
    async def propose(self, *, kind: str, current: BundleView, failures: str, insight: str):
        return BundlePatch(files={"scripts/calc.py": _FIXED})


class _Runner:
    async def run(self, view: BundleView, cases: list[EvalCase]) -> list[EvalOutcome]:
        fixed = _FIXED in view.scripts.values()
        return [EvalOutcome(case_id=c.id, passed=fixed) for c in cases]


class _AgentSigner:
    def __init__(self, did: str, key: bytes) -> None:
        self._did, self._key = did, key

    def sign(self, path: Path, content: bytes) -> None:
        manifest = sign_artifact(content, signer_did=self._did, private_key=self._key)
        path.with_name(path.name + ".arcsig").write_text(manifest.to_json(), encoding="utf-8")


def _skill(root: Path) -> Path:
    sk = root / "s"
    (sk / "scripts").mkdir(parents=True)
    (sk / "evals").mkdir(parents=True)
    (sk / "SKILL.md").write_text("# s\n", encoding="utf-8")
    (sk / "scripts" / "calc.py").write_bytes(_BUGGY)
    (sk / "evals" / "test_g.py").write_text("def test_a():\n    assert 1\n", encoding="utf-8")
    return sk / "SKILL.md"


@pytest.mark.asyncio
async def test_ac6_operator_audit_vs_agent_did_artifact(tmp_path: Path) -> None:
    skill_md = _skill(tmp_path)
    agent = AgentIdentity.generate(org="arc", agent_type="exec")
    operator = OperatorKey.generate()
    assert operator.public_key != agent.public_key

    chain = tmp_path / ".audit" / "skills.worm"
    sink = WormSink(chain, operator.into_signer())
    imp = ArcSkillImprover(
        tmp_path / "ws",
        config=ImproverConfig(min_traces=1, trace_buffer_turns=0, optimize_after_uses=1,
                              min_golden_cases=1),
        tier="enterprise",
        mutator=_FixMutator(),
        eval_runner=_Runner(),
        signer=_AgentSigner(agent.did, agent.signing_seed),
        audit_sink=sink,
        skill_path=lambda name: skill_md,
    )
    await imp.observe(skill_name="s", tool_name="run", status="error", error_type="AssertionError")
    await imp.on_turn_end(turn=0, outcome="failure")
    await imp.maybe_improve()
    await imp.aclose()
    sink.close()

    # Artifact sidecar → AGENT DID.
    sidecar = skill_md.parent / "scripts" / "calc.py.arcsig"
    manifest = ArtifactSignature.from_json(sidecar.read_text(encoding="utf-8"))
    assert manifest.signer_did == agent.did

    # Audit chain → OPERATOR key only. The audited subject cannot forge its own trail.
    assert verify_chain(chain, operator.public_key) is True
    assert verify_chain(chain, agent.public_key) is False


def test_rollback_cools_off_and_audits(tmp_path: Path) -> None:
    ws = tmp_path / "ws"

    class _Sink:
        def __init__(self) -> None:
            self.events: list[object] = []

        def write(self, e: object) -> None:
            self.events.append(e)

    sink = _Sink()
    imp = ArcSkillImprover(ws, config=ImproverConfig(cooloff_turns=100), tier="federal",
                           audit_sink=sink)
    imp._candidate_store.save("sk", Candidate(id="abc123", text="v1\n", generation=1), active=True)

    imp.rollback("sk", "abc123")

    assert imp._candidate_store.load_manifest("sk")["active_candidate_id"] == "abc123"
    assert imp._guardrails.in_cooloff("sk", current_turn=0)  # cooloff engaged
    rolled = [e for e in sink.events if getattr(e, "action", "") == "skill.mutation.rolled_back"]
    assert rolled and getattr(rolled[0], "tier", None) == "federal"


def test_patch_write_confined_to_skill_bundle(tmp_path: Path) -> None:
    """A traversal path in a patch is rejected — no write outside the skill bundle (T7.3)."""
    skill_dir = tmp_path / "s"
    skill_dir.mkdir()
    patch = BundlePatch(files={"../../.audit/forged.worm": b"evil\n"})
    with pytest.raises(ValueError, match="escape"):
        apply_bundle_patch(skill_dir, patch, signer=None)
    assert not (tmp_path / ".audit").exists()
