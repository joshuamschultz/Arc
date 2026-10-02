"""SPEC-044 Phase 7 — audit authority split + reversibility (AC-6, REQ-050/052).

AC-6 (W0-skill, one signing authority): an applied code mutation is committed through
the operator-anchored revision writer (the improver holds no signer and writes no
sidecar), and the WORM audit chain is signed by the OPERATOR key (who-did-what), never
the agent's. Plus: rollback cools off + emits an operator audit; the eval/patch paths
never touch operator/.audit locations.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arcskill.improver import ArcSkillImprover, ImproverConfig
from arcskill.improver.codepatch import apply_bundle_patch
from arcskill.improver.models import BundlePatch, BundleView, Candidate, EvalCase, EvalOutcome
from arctrust import OperatorKey, WormSink, verify_chain
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


async def _auto_approve(action: str, skill_name: str, detail: str) -> bool:
    return True


def _skill(root: Path) -> Path:
    sk = root / "s"
    (sk / "scripts").mkdir(parents=True)
    (sk / "evals").mkdir(parents=True)
    (sk / "SKILL.md").write_text("# s\n", encoding="utf-8")
    (sk / "scripts" / "calc.py").write_bytes(_BUGGY)
    (sk / "evals" / "test_g.py").write_text("def test_a():\n    assert 1\n", encoding="utf-8")
    return sk / "SKILL.md"


@pytest.mark.asyncio
async def test_ac6_operator_audit_and_writer_only_artifact(
    tmp_path: Path, dir_writer: Any
) -> None:
    skill_md = _skill(tmp_path)
    agent = AgentIdentity.generate(org="arc", agent_type="exec")
    operator = OperatorKey.generate()
    assert operator.public_key != agent.public_key

    writer = dir_writer(lambda _name: tmp_path / "committed")
    chain = tmp_path / ".audit" / "skills.worm"
    sink = WormSink(chain, operator.into_signer())
    imp = ArcSkillImprover(
        tmp_path / "ws",
        config=ImproverConfig(
            min_traces=1, trace_buffer_turns=0, optimize_after_uses=1, min_golden_cases=1
        ),
        tier="enterprise",
        mutator=_FixMutator(),
        eval_runner=_Runner(),
        writer=writer,
        approval_provider=_auto_approve,  # enterprise code mutation requires approval (D-10)
        audit_sink=sink,
        agent_did=agent.did,
        skill_path=lambda name: skill_md,
    )
    await imp.observe(skill_name="s", tool_name="run", status="error", error_type="AssertionError")
    await imp.on_turn_end(turn=0, outcome="failure")
    await imp.maybe_improve()
    await imp.aclose()
    sink.close()

    # The patch reached the skill only through the writer; the improver signed nothing.
    assert [(name, files) for name, files, _ in writer.commits] == [
        ("s", {"scripts/calc.py": _FIXED})
    ]
    assert (skill_md.parent / "scripts" / "calc.py").read_bytes() == _BUGGY
    assert not list(tmp_path.rglob("*.arcsig"))

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
    imp = ArcSkillImprover(
        ws, config=ImproverConfig(cooloff_turns=100), tier="federal", audit_sink=sink
    )
    imp._candidate_store.save("sk", Candidate(id="abc123", text="v1\n", generation=1), active=True)

    imp.rollback("sk", "abc123")

    assert imp._candidate_store.load_manifest("sk")["active_candidate_id"] == "abc123"
    assert imp._guardrails.in_cooloff("sk", current_turn=0)  # cooloff engaged
    rolled = [e for e in sink.events if getattr(e, "action", "") == "skill.mutation.rolled_back"]
    assert rolled and getattr(rolled[0], "tier", None) == "federal"


def test_patch_write_confined_to_skill_bundle(tmp_path: Path, dir_writer: Any) -> None:
    """A traversal path in a patch is rejected — no write outside the skill bundle (T7.3)."""
    writer = dir_writer(lambda _name: tmp_path / "s")
    patch = BundlePatch(files={"../../.audit/forged.worm": b"evil\n"})
    with pytest.raises(ValueError, match="escape"):
        apply_bundle_patch("s", patch, writer=writer)
    assert writer.commits == []
    assert not (tmp_path / ".audit").exists()
