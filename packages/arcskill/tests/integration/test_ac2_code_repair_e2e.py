"""AC-2 — code-repair E2E through the REAL production path (SPEC-044, the headline).

The producers-unwired defense in one test. A skill ships with a **seeded code bug** and
a golden-task suite. Driving only the primitive ``SkillAdapter`` signals the arcagent
extension forwards (observe → on_turn_end → maybe_improve), the improver:

1. accrues usage + failing traces via the real trace store;
2. proposes a code patch (the LLM is the one injected seam — a real fix, deterministic);
3. runs the golden suite in the **REAL Docker sandbox** (HubEvalRunner, not injected);
4. accepts only on strict improvement (buggy fails → fixed passes);
5. writes the patch, **re-signs every file with a real agent-DID key (arctrust)**;
6. **re-verifies** the signature (fail-closed) before reload;
7. reloads; the previously-failing golden case now passes when re-run in the sandbox;
8. emits a mutation audit event on the WORM sink.

No rigged fixtures, no direct ``engine.optimize`` call. Skipped without Docker.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arcskill.improver import ArcSkillImprover, ImproverConfig
from arcskill.improver.models import BundlePatch, BundleView
from arcskill.improver.sandbox_runner import HubEvalRunner, docker_available
from arctrust import sign_artifact, verify_artifact
from arctrust.artifact import ArtifactSignature
from arctrust.identity import AgentIdentity

pytestmark = pytest.mark.skipif(not docker_available(), reason="docker CLI not available")

_BUGGY = b"def add(a, b):\n    return a - b\n"  # seeded bug: subtracts
_FIXED = b"def add(a, b):\n    return a + b\n"
_GOLDEN = (
    "from calc import add\n\n"
    "def test_a():\n    assert add(2, 3) == 5\n\n"
    "def test_b():\n    assert add(4, 1) == 5\n\n"
    "def test_c():\n    assert add(10, 20) == 30\n"
)


class _FixMutator:
    """The injected LLM seam — returns a real, correct code fix (deterministic)."""

    async def propose(self, *, kind: str, current: BundleView, failures: str, insight: str):
        assert kind == "code" and "scripts/calc.py" in current.scripts
        return BundlePatch(files={"scripts/calc.py": _FIXED}, summary="add sums, not subtracts")


class _AgentSigner:
    """Real agent-DID sidecar signer (the shape arcagent injects), arctrust-backed."""

    def __init__(self, did: str, key: bytes) -> None:
        self._did, self._key = did, key

    def sign(self, path: Path, content: bytes) -> None:
        manifest = sign_artifact(content, signer_did=self._did, private_key=self._key)
        path.with_name(path.name + ".arcsig").write_text(manifest.to_json(), encoding="utf-8")


class _CaptureSink:
    def __init__(self) -> None:
        self.events: list[object] = []

    def write(self, event: object) -> None:
        self.events.append(event)


class _AutoApprover:
    """Operator-approval seam — grants the enterprise code mutation (D-10)."""

    async def request(self, *, action: str, skill_name: str, detail: str) -> bool:
        return True


def _seed_skill(root: Path) -> Path:
    sk = root / "skills" / "calc-skill"
    (sk / "scripts").mkdir(parents=True)
    (sk / "evals").mkdir(parents=True)
    (sk / "SKILL.md").write_text("# Calc skill\n", encoding="utf-8")
    (sk / "scripts" / "calc.py").write_bytes(_BUGGY)
    (sk / "evals" / "test_calc.py").write_text(_GOLDEN, encoding="utf-8")
    return sk / "SKILL.md"


@pytest.mark.asyncio
async def test_ac2_seeded_bug_repaired_through_real_path(tmp_path: Path) -> None:
    skill_md = _seed_skill(tmp_path)
    ident = AgentIdentity.generate(org="arc", agent_type="exec")
    sink = _CaptureSink()
    reloaded: list[bool] = []

    imp = ArcSkillImprover(
        tmp_path / "ws",
        config=ImproverConfig(min_traces=1, trace_buffer_turns=0, optimize_after_uses=1),
        tier="enterprise",  # real Docker sandbox; NOT injecting eval_runner
        mutator=_FixMutator(),
        signer=_AgentSigner(ident.did, ident.signing_seed),
        approver=_AutoApprover(),  # enterprise code mutation requires operator approval
        audit_sink=sink,
        agent_did=ident.did,
        skill_path=lambda name: skill_md,
        reload=lambda: reloaded.append(True),
    )

    # Drive ONLY the primitive SkillAdapter surface the extension calls.
    await imp.observe(
        skill_name="calc-skill", tool_name="run", status="error", error_type="AssertionError"
    )
    await imp.on_turn_end(turn=0, outcome="failure")
    await imp.maybe_improve()
    await imp.aclose()

    calc = skill_md.parent / "scripts" / "calc.py"
    sidecar = skill_md.parent / "scripts" / "calc.py.arcsig"

    # 5+6: patch written, re-signed by the agent DID, signature re-verifies.
    assert calc.read_bytes() == _FIXED
    manifest = ArtifactSignature.from_json(sidecar.read_text(encoding="utf-8"))
    assert manifest.signer_did == ident.did
    assert verify_artifact(_FIXED, manifest) is True

    # 7: reloaded, and the previously-failing golden suite now passes in the REAL sandbox.
    assert reloaded == [True]
    runner = HubEvalRunner(tier="enterprise", timeout_s=60)
    from arcskill.improver.models import EvalCase

    cases = [
        EvalCase(id=f"evals/test_calc.py::test_{x}", node=f"evals/test_calc.py::test_{x}")
        for x in ("a", "b", "c")
    ]
    outcomes = await runner.run(BundleView("calc-skill", "# Calc skill\n", skill_md.parent), cases)
    assert all(o.passed for o in outcomes), [(o.case_id, o.detail) for o in outcomes]

    # 8: a mutation audit event landed on the WORM sink.
    assert any(getattr(e, "action", "") == "skill.mutation.applied" for e in sink.events)
