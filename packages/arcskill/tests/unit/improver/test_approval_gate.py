"""SPEC-044 CRITICAL-2 — operator-approval (HITL, D-10) gate on code mutations.

The tier ladder (PRD §7): personal auto; enterprise + federal require operator approval
for a CODE mutation before it applies. Fail-closed when approval is required but no
approver is wired. Every approval decision is an operator-signed audit event. Retire/revive
approval is covered in ``test_lifecycle.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arcskill.improver import ArcSkillImprover, ImproverConfig
from arcskill.improver.models import BundlePatch, BundleView, EvalCase, EvalOutcome

_BUGGY = b"def add(a, b):\n    return a - b\n"
_FIXED = b"def add(a, b):\n    return a + b\n"


class _FixMutator:
    async def propose(self, *, kind: str, current: BundleView, failures: str, insight: str):
        return BundlePatch(files={"scripts/calc.py": _FIXED}, summary="fix add")


class _PassRunner:
    """Golden suite fails on the buggy bundle, passes on the fixed one (strict improvement)."""

    async def run(self, view: BundleView, cases: list[EvalCase]) -> list[EvalOutcome]:
        fixed = _FIXED in view.scripts.values()
        return [EvalOutcome(case_id=c.id, passed=fixed) for c in cases]


class _Approver:
    """Callable ApprovalProvider (action, skill_name, detail) -> approved."""

    def __init__(self, grant: bool) -> None:
        self.grant = grant
        self.calls: list[str] = []

    async def __call__(self, action: str, skill_name: str, detail: str) -> bool:
        self.calls.append(action)
        return self.grant


class _Sink:
    def __init__(self) -> None:
        self.events: list[object] = []

    def write(self, event: object) -> None:
        self.events.append(event)


def _seed(root: Path) -> Path:
    sk = root / "s"
    (sk / "scripts").mkdir(parents=True)
    (sk / "evals").mkdir(parents=True)
    (sk / "SKILL.md").write_text("# s\n", encoding="utf-8")
    (sk / "scripts" / "calc.py").write_bytes(_BUGGY)
    (sk / "evals" / "test_g.py").write_text("def test_a():\n    assert 1\n", encoding="utf-8")
    return sk / "SKILL.md"


def _make(
    root: Path, skill_md: Path, *, tier: str, approval_provider: object | None, sink: _Sink
) -> ArcSkillImprover:
    return ArcSkillImprover(
        root / "ws",
        config=ImproverConfig(
            min_traces=1, trace_buffer_turns=0, optimize_after_uses=1, min_golden_cases=1
        ),
        tier=tier,
        mutator=_FixMutator(),
        eval_runner=_PassRunner(),
        approval_provider=approval_provider,
        audit_sink=sink,
        skill_path=lambda name: skill_md,
    )


async def _drive(imp: ArcSkillImprover) -> None:
    await imp.observe(skill_name="s", tool_name="run", status="error", error_type="AssertionError")
    await imp.on_turn_end(turn=0, outcome="failure")
    await imp.maybe_improve()
    await imp.aclose()


@pytest.mark.asyncio
async def test_enterprise_code_blocked_without_approver(tmp_path: Path) -> None:
    """Enterprise code mutation with no approver fails closed: patch NOT applied, audited."""
    skill_md = _seed(tmp_path)
    sink = _Sink()
    imp = _make(tmp_path, skill_md, tier="enterprise", approval_provider=None, sink=sink)

    await _drive(imp)

    assert (skill_md.parent / "scripts" / "calc.py").read_bytes() == _BUGGY  # unchanged
    denied = [e for e in sink.events if getattr(e, "outcome", "") == "denied_no_approver"]
    assert denied, "the blocked code mutation must be audited"


@pytest.mark.asyncio
async def test_enterprise_code_applies_when_approved(tmp_path: Path) -> None:
    skill_md = _seed(tmp_path)
    sink = _Sink()
    approver = _Approver(grant=True)
    imp = _make(tmp_path, skill_md, tier="enterprise", approval_provider=approver, sink=sink)

    await _drive(imp)

    assert (skill_md.parent / "scripts" / "calc.py").read_bytes() == _FIXED  # applied
    assert "skill.mutation" in approver.calls
    approved = [e for e in sink.events if getattr(e, "outcome", "") == "approved"]
    assert approved


@pytest.mark.asyncio
async def test_federal_code_blocked_when_approver_denies(tmp_path: Path) -> None:
    skill_md = _seed(tmp_path)
    sink = _Sink()
    imp = _make(
        tmp_path, skill_md, tier="federal", approval_provider=_Approver(grant=False), sink=sink
    )

    await _drive(imp)

    assert (skill_md.parent / "scripts" / "calc.py").read_bytes() == _BUGGY  # unchanged
    denied = [e for e in sink.events if getattr(e, "outcome", "") == "denied"]
    assert denied


@pytest.mark.asyncio
async def test_personal_code_needs_no_approval(tmp_path: Path) -> None:
    """Personal tier auto-applies (audited) — no approver required (PRD §7)."""
    skill_md = _seed(tmp_path)
    sink = _Sink()
    imp = _make(tmp_path, skill_md, tier="personal", approval_provider=None, sink=sink)

    await _drive(imp)

    assert (skill_md.parent / "scripts" / "calc.py").read_bytes() == _FIXED  # applied, no approver
