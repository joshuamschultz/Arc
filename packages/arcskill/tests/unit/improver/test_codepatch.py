"""SPEC-044 Phase 4 — code-repair mutation path (REQ-010/011/012/016).

Two levels:
* the ``ArcSkillImprover`` facade drives the *whole* code path from primitive signals
  (observe → on_turn_end → maybe_improve) through propose → golden-gate → apply →
  reload — no direct ``engine.optimize`` call (producers-unwired defense);
* ``apply_bundle_patch`` fails closed when the agent-DID re-verification does not hold,
  restoring the original bytes (REQ-012).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arcskill.improver import ArcSkillImprover, ImproverConfig
from arcskill.improver.codepatch import BundleReverifyError, apply_bundle_patch
from arcskill.improver.models import BundlePatch, BundleView, EvalCase, EvalOutcome
from arctrust import sign_artifact
from arctrust.artifact import ArtifactSignature
from arctrust.identity import AgentIdentity

_BUGGY = b"def add(a, b):\n    return a - b\n"
_FIXED = b"def add(a, b):\n    return a + b\n"
_GOLDEN = "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
_NODE = "evals/test_calc.py::test_add"


class _FakeMutator:
    def __init__(self, patch: BundlePatch) -> None:
        self._patch = patch

    async def propose(self, *, kind: str, current: BundleView, failures: str, insight: str):
        assert kind == "code"
        return self._patch


class _FakeRunner:
    """before (buggy scripts) → fail; after (fixed overlay) → pass — no real sandbox."""

    async def run(self, view: BundleView, cases: list[EvalCase]) -> list[EvalOutcome]:
        fixed = _FIXED in view.scripts.values()
        return [EvalOutcome(case_id=c.id, passed=fixed) for c in cases]


class _ArctrustSigner:
    def __init__(self, did: str, key: bytes, *, tamper: bool = False) -> None:
        self._did, self._key, self._tamper = did, key, tamper

    def sign(self, path: Path, content: bytes) -> None:
        signed = content + b"x" if self._tamper else content
        manifest = sign_artifact(signed, signer_did=self._did, private_key=self._key)
        path.with_name(path.name + ".arcsig").write_text(manifest.to_json(), encoding="utf-8")


def _make_skill(root: Path) -> Path:
    sk = root / "skill_traces_ignored" / "calc-skill"
    (sk / "scripts").mkdir(parents=True)
    (sk / "evals").mkdir(parents=True)
    (sk / "SKILL.md").write_text("# Calc\n", encoding="utf-8")
    (sk / "scripts" / "calc.py").write_bytes(_BUGGY)
    (sk / "evals" / "test_calc.py").write_text(_GOLDEN, encoding="utf-8")
    return sk / "SKILL.md"


@pytest.mark.asyncio
async def test_facade_drives_code_repair_end_to_end(tmp_path: Path) -> None:
    """observe→improve applies a gated code patch and reloads — the real facade path."""
    skill_md = _make_skill(tmp_path)
    reloaded: list[bool] = []
    imp = ArcSkillImprover(
        tmp_path / "ws",
        config=ImproverConfig(
            min_traces=1, trace_buffer_turns=0, optimize_after_uses=1, min_golden_cases=1
        ),
        tier="personal",
        mutator=_FakeMutator(BundlePatch(files={"scripts/calc.py": _FIXED}, summary="fix add")),
        eval_runner=_FakeRunner(),
        skill_path=lambda name: skill_md,
        reload=lambda: reloaded.append(True),
    )

    await imp.observe(
        skill_name="calc-skill", tool_name="run", status="error", error_type="AssertionError"
    )
    await imp.on_turn_end(turn=0, outcome="failure")
    await imp.maybe_improve()
    await imp.aclose()

    assert (skill_md.parent / "scripts" / "calc.py").read_bytes() == _FIXED
    assert reloaded == [True]


@pytest.mark.asyncio
async def test_facade_rejects_patch_that_does_not_fix_suite(tmp_path: Path) -> None:
    """A patch that fails the golden gate is never applied (strict improvement)."""
    skill_md = _make_skill(tmp_path)
    reloaded: list[bool] = []
    imp = ArcSkillImprover(
        tmp_path / "ws",
        config=ImproverConfig(
            min_traces=1, trace_buffer_turns=0, optimize_after_uses=1, min_golden_cases=1
        ),
        tier="personal",
        # Patch keeps the bug → runner reports fail before AND after → no improvement.
        mutator=_FakeMutator(BundlePatch(files={"scripts/calc.py": _BUGGY}, summary="noop")),
        eval_runner=_FakeRunner(),
        skill_path=lambda name: skill_md,
        reload=lambda: reloaded.append(True),
    )
    await imp.observe(
        skill_name="calc-skill", tool_name="run", status="error", error_type="AssertionError"
    )
    await imp.on_turn_end(turn=0, outcome="failure")
    await imp.maybe_improve()
    await imp.aclose()

    assert (skill_md.parent / "scripts" / "calc.py").read_bytes() == _BUGGY  # unchanged
    assert reloaded == []


def test_apply_bundle_patch_signs_and_reverifies(tmp_path: Path) -> None:
    """A valid agent-DID signature re-verifies; the sidecar is written beside the file."""
    skill_dir = tmp_path / "s"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "scripts" / "calc.py").write_bytes(_BUGGY)
    ident = AgentIdentity.generate(org="arc", agent_type="exec")
    patch = BundlePatch(files={"scripts/calc.py": _FIXED})

    apply_bundle_patch(skill_dir, patch, signer=_ArctrustSigner(ident.did, ident.signing_seed))

    assert (skill_dir / "scripts" / "calc.py").read_bytes() == _FIXED
    sidecar = skill_dir / "scripts" / "calc.py.arcsig"
    manifest = ArtifactSignature.from_json(sidecar.read_text(encoding="utf-8"))
    assert manifest.signer_did == ident.did


def test_apply_bundle_patch_fails_closed_on_bad_signature(tmp_path: Path) -> None:
    """A mismatched signature raises and the original bytes are restored (REQ-012)."""
    skill_dir = tmp_path / "s"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "scripts" / "calc.py").write_bytes(_BUGGY)
    ident = AgentIdentity.generate(org="arc", agent_type="exec")
    patch = BundlePatch(files={"scripts/calc.py": _FIXED})

    with pytest.raises(BundleReverifyError):
        apply_bundle_patch(
            skill_dir, patch, signer=_ArctrustSigner(ident.did, ident.signing_seed, tamper=True)
        )

    assert (skill_dir / "scripts" / "calc.py").read_bytes() == _BUGGY  # rolled back
