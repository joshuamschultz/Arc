"""Human-authored golden suite for the daily-brief skill (H-042 seed).

These are real acceptance cases, not scaffolding: each pins a load-bearing property of
the SKILL.md contract so a prose "improvement" from ``arcskill.improver`` can never
silently drop it. This module's docstring deliberately carries no machine-authorship
marker, so ``arcskill.improver.evalgate`` classifies every case here as human-authored —
counting toward ``min_golden_cases`` at every tier (personal/enterprise/federal), not
just as supplemental machine coverage.

Written against the skill's own contract (see ``../SKILL.md``): the five-item cap, the
fact/inference labeling distinction, and the quiet-day short-circuit are the three
properties that make this skill's output trustworthy rather than padded. Losing any one
of them is a regression the gate must catch.
"""

from __future__ import annotations

from pathlib import Path

# The improver's sandbox (arcskill.improver.sandbox_runner) materializes a candidate's
# SKILL.md text as this same relative path before running these cases — so a candidate
# mutation is judged on ITS OWN body, not a frozen snapshot.
_SKILL_MD = Path(__file__).resolve().parent.parent / "SKILL.md"


def _text() -> str:
    return _SKILL_MD.read_text(encoding="utf-8").lower()


def test_contract_caps_at_five_items():
    """The brief's whole value proposition is what it leaves out — losing the cap
    turns it back into an undifferentiated digest."""
    text = _text()
    assert "five items" in text or "5 items" in text


def test_contract_labels_fact_vs_inference():
    """Facts and inferences must stay visibly distinct in the output contract — an
    inference stated as a fact is the skill's own documented Red Flag."""
    text = _text()
    assert "you told me" in text
    assert "it looks like" in text


def test_quiet_day_short_circuits():
    """On a quiet day the brief must say so in one line and stop — the padding this
    skill exists to prevent."""
    text = _text()
    assert "quiet day" in text
    assert "one line" in text or "one sentence" in text
