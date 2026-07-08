"""Unit tests for the concrete sandboxed golden-task runner (SPEC-044 P3.3).

``HubEvalRunner`` is the default production ``EvalRunner``: it materializes a
:class:`BundleView` into a bundle directory, runs the golden cases in the
tier-appropriate sandbox (``arcskill.hub`` — Firecracker federal / Docker
fallback), and parses per-case pass/fail.

These tests exercise the parse logic and the two non-Docker branches that are
deterministically reachable on any host:

* **personal host-fallback** — no sandbox available → the golden harness runs on
  the host via subprocess (audit-warn). This genuinely executes the harness
  end-to-end, so a seeded-bug script fails and its fixed overlay passes.
* **fail-closed** — federal/enterprise tier with no sandbox → ``SandboxRequired``.

The real-Docker path is covered by ``tests/integration/test_eval_runner_docker.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arcskill.hub.errors import SandboxRequired
from arcskill.improver.models import BundleView, EvalCase
from arcskill.improver.sandbox_runner import HubEvalRunner, _parse_outcomes

_BUGGY_CALC = b"def add(a, b):\n    return a - b\n"  # seeded bug: subtracts
_FIXED_CALC = b"def add(a, b):\n    return a + b\n"
_GOLDEN_TEST = (
    "from calc import add\n"
    "\n"
    "def test_add_two_and_three():\n"
    "    assert add(2, 3) == 5\n"
    "\n"
    "def test_add_zero():\n"
    "    assert add(0, 0) == 0\n"
)

_NODE_SUM = "evals/test_calc.py::test_add_two_and_three"
_NODE_ZERO = "evals/test_calc.py::test_add_zero"


def _make_skill(tmp_path: Path) -> Path:
    """Create a skill dir with a buggy ``scripts/calc.py`` + golden eval suite."""
    skill_dir = tmp_path / "calc-skill"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "evals").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Calc skill\n", encoding="utf-8")
    (skill_dir / "scripts" / "calc.py").write_bytes(_BUGGY_CALC)
    (skill_dir / "evals" / "test_calc.py").write_text(_GOLDEN_TEST, encoding="utf-8")
    return skill_dir


def _cases() -> list[EvalCase]:
    return [EvalCase(id=_NODE_SUM, node=_NODE_SUM), EvalCase(id=_NODE_ZERO, node=_NODE_ZERO)]


def test_parse_outcomes_maps_lines_to_cases() -> None:
    """Harness ``ARC_EVAL`` lines map to per-case pass/fail; missing → fail."""
    stdout = (
        "noise\n"
        f"ARC_EVAL\t{_NODE_SUM}\tPASS\n"
        f"ARC_EVAL\t{_NODE_ZERO}\tFAIL\tAssertionError\n"
    )
    outcomes = _parse_outcomes(stdout, _cases())
    by_id = {o.case_id: o for o in outcomes}
    assert by_id[_NODE_SUM].passed is True
    assert by_id[_NODE_ZERO].passed is False
    assert "AssertionError" in by_id[_NODE_ZERO].detail


def test_parse_outcomes_missing_line_is_failure() -> None:
    """A case with no harness line is a conservative failure (fail-closed)."""
    outcomes = _parse_outcomes(f"ARC_EVAL\t{_NODE_SUM}\tPASS\n", _cases())
    by_id = {o.case_id: o for o in outcomes}
    assert by_id[_NODE_SUM].passed is True
    assert by_id[_NODE_ZERO].passed is False


@pytest.mark.asyncio
async def test_personal_host_fallback_detects_seeded_bug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Personal tier, no sandbox → host harness runs; buggy code fails the golden case."""
    monkeypatch.setattr("arcskill.improver.sandbox_runner.is_firecracker_available", lambda: False)
    monkeypatch.setattr("arcskill.improver.sandbox_runner.docker_available", lambda: False)
    skill_dir = _make_skill(tmp_path)
    runner = HubEvalRunner(tier="personal")

    view = BundleView("calc", "# Calc skill\n", skill_dir)
    with caplog.at_level("WARNING"):
        outcomes = await runner.run(view, _cases())

    by_id = {o.case_id: o for o in outcomes}
    assert by_id[_NODE_SUM].passed is False  # add(2,3) == -1 with the seeded bug
    assert by_id[_NODE_ZERO].passed is True  # add(0,0) == 0 regardless
    assert any("host" in r.message.lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_personal_host_fallback_fixed_overlay_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``scripts`` overlay carrying the fix flips the failing golden case to pass."""
    monkeypatch.setattr("arcskill.improver.sandbox_runner.is_firecracker_available", lambda: False)
    monkeypatch.setattr("arcskill.improver.sandbox_runner.docker_available", lambda: False)
    skill_dir = _make_skill(tmp_path)
    runner = HubEvalRunner(tier="personal")

    view = BundleView(
        "calc", "# Calc skill\n", skill_dir, scripts={"scripts/calc.py": _FIXED_CALC}
    )
    outcomes = await runner.run(view, _cases())

    assert all(o.passed for o in outcomes)


@pytest.mark.asyncio
async def test_federal_no_sandbox_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Federal tier with no Firecracker → SandboxRequired (never degrade to host)."""
    monkeypatch.setattr("arcskill.improver.sandbox_runner.is_firecracker_available", lambda: False)
    monkeypatch.setattr("arcskill.improver.sandbox_runner.docker_available", lambda: False)
    skill_dir = _make_skill(tmp_path)
    runner = HubEvalRunner(tier="federal")

    with pytest.raises(SandboxRequired):
        await runner.run(BundleView("calc", "# Calc skill\n", skill_dir), _cases())


@pytest.mark.asyncio
async def test_enterprise_no_sandbox_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enterprise tier with no sandbox → fail-closed (host fallback is personal-only)."""
    monkeypatch.setattr("arcskill.improver.sandbox_runner.is_firecracker_available", lambda: False)
    monkeypatch.setattr("arcskill.improver.sandbox_runner.docker_available", lambda: False)
    skill_dir = _make_skill(tmp_path)
    runner = HubEvalRunner(tier="enterprise")

    with pytest.raises(SandboxRequired):
        await runner.run(BundleView("calc", "# Calc skill\n", skill_dir), _cases())
