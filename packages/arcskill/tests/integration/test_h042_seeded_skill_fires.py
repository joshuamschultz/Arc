"""H-042 — proves the improver loop actually FIRES against a real seeded skill.

Before H-042, zero shipped skills carried an ``evals/`` suite, so every prose gate
call hit ``no_suite_policy`` and every code-repair attempt blocked outright — the
improver was wired end-to-end but never had a golden suite to run. This test drives
the REAL production pieces (``load_suite``, ``EvalGate``, ``HubEvalRunner`` — the
same Docker/Firecracker sandbox the code-repair E2E test uses) against the REAL
skill directory ``blueprints/personal-assistant/skills/daily-brief``, whose
``evals/test_golden.py`` was seeded by H-042. No rigged fixture: the golden cases,
the SKILL.md text, and the gate are all the genuine artifacts an operator ships.

Skipped without Docker/Firecracker, matching ``test_ac2_code_repair_e2e.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from arcskill.hub.dry_run import is_firecracker_available
from arcskill.improver.evalgate import EvalGate, load_suite
from arcskill.improver.models import BundleView
from arcskill.improver.sandbox_runner import HubEvalRunner, docker_available

pytestmark = pytest.mark.skipif(
    not (docker_available() or is_firecracker_available()),
    reason="no sandbox (Docker/Firecracker) available",
)

_SKILL_DIR = (
    Path(__file__).resolve().parents[4] / "blueprints" / "personal-assistant" / "skills"
    / "daily-brief"
)


def _strip_quiet_day_short_circuit(text: str) -> str:
    """Remove every trace of the quiet-day short-circuit property (case-insensitive,
    every occurrence) — exactly what ``test_quiet_day_short_circuits`` pins. Everything
    else (five-item cap, fact/inference labeling) is left intact, so only ONE golden
    case should flip when this is restored."""
    text = re.sub(r"quiet day", "slow day", text, flags=re.IGNORECASE)
    text = re.sub(r"one (line|sentence)", "a short paragraph", text, flags=re.IGNORECASE)
    return text


# A deliberately regressed "before" candidate — see docstring above.
_REGRESSED_TEXT = _strip_quiet_day_short_circuit(
    (_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
)


def test_seed_shipped_a_real_human_authored_suite() -> None:
    """The precondition H-042 exists to establish: this skill has a live golden suite."""
    cases = load_suite(_SKILL_DIR)
    assert len(cases) >= 3
    assert all(not c.machine_authored for c in cases), (
        "seeded cases must count toward min_golden_cases at every tier"
    )


@pytest.mark.asyncio
async def test_gate_fires_fail_to_pass_with_zero_regressions() -> None:
    """The real gate, the real sandbox, the real skill: fail -> pass, no collateral damage."""
    cases = load_suite(_SKILL_DIR)
    good_text = (_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert _REGRESSED_TEXT != good_text, "the regressed fixture must actually differ"

    runner = HubEvalRunner(tier="personal")
    gate = EvalGate(runner, min_golden_cases=3)
    decision = await gate.decide(
        before=BundleView("daily-brief", _REGRESSED_TEXT, _SKILL_DIR),
        after=BundleView("daily-brief", good_text, _SKILL_DIR),
        cases=cases,
        tier="personal",
        kind="prose",
    )

    assert decision.accepted, decision.reason
    assert decision.newly_passing == 1  # exactly the quiet-day case flips
    assert decision.after_pass == len(cases)  # nothing else regressed


@pytest.mark.asyncio
async def test_gate_rejects_when_the_candidate_is_no_better(tmp_path: Path) -> None:
    """Sanity check on the other side of the same real path: no change, no acceptance."""
    cases = load_suite(_SKILL_DIR)
    good_text = (_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

    runner = HubEvalRunner(tier="personal")
    gate = EvalGate(runner, min_golden_cases=3)
    decision = await gate.decide(
        before=BundleView("daily-brief", good_text, _SKILL_DIR),
        after=BundleView("daily-brief", good_text, _SKILL_DIR),
        cases=cases,
        tier="personal",
        kind="prose",
    )

    assert decision.accepted is False
    assert "no strict improvement" in decision.reason
