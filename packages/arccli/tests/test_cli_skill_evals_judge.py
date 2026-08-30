"""H-041c — `arc skill evals judge` runs the REAL pinned judge, fail-closed.

Wires ``ArcSkillImprover.evaluate_curated`` (the one place a real judge verdict is
computed) to a CLI verb the reviewing operator runs on a candidate the auto-improver
routed to review. In-process (not the subprocess harness the sibling tests use) so a
deterministic judge can be injected in place of a live LLM.

Pins: a pinned judge (judge_model_id + rubric_sha256) produces a REAL recorded verdict,
and a non-"pass" verdict exits nonzero (fail-closed — LLM09).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
from arcskill.improver import CuratedGoldenCase, rubric_digest
from arcskill.improver.curation import emit_golden_case

from arccli.commands import skill_evals

_RUBRIC = "Pass iff the summary names the customer and the amount."


class _FakeJudge:
    """Deterministic stand-in for the pinned LLM judge (structural LLMInvoker)."""

    def __init__(self, verdict: str) -> None:
        self._verdict = verdict

    async def invoke(self, prompt: str) -> str:
        return self._verdict


def _skill_with_judge_case(tmp_path: Path) -> Path:
    skill_dir = tmp_path / "s"
    (skill_dir / "evals").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# s\nSummarize an invoice.\n", encoding="utf-8")
    emit_golden_case(
        skill_dir,
        CuratedGoldenCase(
            case_id="quality",
            skill_name="s",
            gate_type="judge_rubric",
            rubric=_RUBRIC,
            judge_model_id="anthropic:claude-haiku",
            rubric_sha256=rubric_digest(_RUBRIC),
        ),
    )
    return skill_dir


def _ns(skill_dir: Path, output: Path) -> argparse.Namespace:
    return argparse.Namespace(
        target=["judge", str(skill_dir), str(output)], json=True, force=False, yes=False
    )


def test_judge_produces_real_pinned_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A passing pinned judge yields a real verdict recording the judge id + rubric sha256."""
    skill_dir = _skill_with_judge_case(tmp_path)
    output = tmp_path / "candidate.txt"
    output.write_text("Customer Acme owes $42.", encoding="utf-8")

    monkeypatch.setattr(skill_evals, "_make_judge", lambda model_id: _FakeJudge("PASS"))
    skill_evals.evals_handler(_ns(skill_dir, output))

    verdicts = json.loads(capsys.readouterr().out)
    assert len(verdicts) == 1
    assert verdicts[0]["passed"] is True
    assert verdicts[0]["judge_model_id"] == "anthropic:claude-haiku"
    assert verdicts[0]["rubric_sha256"] == rubric_digest(_RUBRIC)


def test_judge_failing_verdict_is_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-'pass' judge verdict exits nonzero — the candidate is not waved through."""
    skill_dir = _skill_with_judge_case(tmp_path)
    output = tmp_path / "candidate.txt"
    output.write_text("unrelated text", encoding="utf-8")

    monkeypatch.setattr(skill_evals, "_make_judge", lambda model_id: _FakeJudge("FAIL"))
    with pytest.raises(SystemExit) as exc:
        skill_evals.evals_handler(_ns(skill_dir, output))
    assert exc.value.code == 1
