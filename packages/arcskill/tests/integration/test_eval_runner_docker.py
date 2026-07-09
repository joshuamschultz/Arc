"""Integration: HubEvalRunner runs golden cases in a REAL Docker sandbox (SPEC-044 P3.3).

Proves the concrete eval runner is not a rigged fixture — the golden-task suite is
materialized and executed inside an actual Docker container (network-isolated,
cap-dropped, read-only root), and per-case pass/fail is parsed from the harness output.
The seeded-bug script fails its golden case; the fixed overlay flips it to pass.

Skipped when the ``docker`` CLI is absent.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arcskill.improver.models import BundleView, EvalCase
from arcskill.improver.sandbox_runner import HubEvalRunner, docker_available

pytestmark = pytest.mark.skipif(not docker_available(), reason="docker CLI not available")

_BUGGY_CALC = b"def add(a, b):\n    return a - b\n"
_FIXED_CALC = b"def add(a, b):\n    return a + b\n"
_GOLDEN_TEST = "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
_NODE = "evals/test_calc.py::test_add"


def _make_skill(tmp_path: Path) -> Path:
    skill_dir = tmp_path / "calc-skill"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "evals").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Calc\n", encoding="utf-8")
    (skill_dir / "scripts" / "calc.py").write_bytes(_BUGGY_CALC)
    (skill_dir / "evals" / "test_calc.py").write_text(_GOLDEN_TEST, encoding="utf-8")
    return skill_dir


@pytest.mark.asyncio
async def test_docker_runner_seeded_bug_fails_then_fixed_overlay_passes(tmp_path: Path) -> None:
    """Enterprise tier uses the real Docker sandbox: bug fails, fix passes — per case."""
    skill_dir = _make_skill(tmp_path)
    runner = HubEvalRunner(tier="enterprise", timeout_s=60)
    cases = [EvalCase(id=_NODE, node=_NODE)]

    before = await runner.run(BundleView("calc", "# Calc\n", skill_dir), cases)
    assert before[0].passed is False, before[0].detail

    after = await runner.run(
        BundleView("calc", "# Calc\n", skill_dir, scripts={"scripts/calc.py": _FIXED_CALC}),
        cases,
    )
    assert after[0].passed is True, after[0].detail
