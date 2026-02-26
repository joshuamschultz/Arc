"""Tests for Guardrails — eligibility checks, candidate validation, safety enforcement."""

from __future__ import annotations

from datetime import UTC, datetime

from arcagent.modules.skill_improver.config import SkillImproverConfig
from arcagent.modules.skill_improver.guardrails import Guardrails
from arcagent.modules.skill_improver.models import Candidate, SkillTrace, ToolCallRecord


def _make_trace(skill_name: str = "test-skill", turn: int = 0) -> SkillTrace:
    return SkillTrace(
        trace_id=f"trace-{turn}",
        session_id="s1",
        skill_name=skill_name,
        skill_version=0,
        turn_number=turn,
        started_at=datetime(2026, 2, 25, 10, 0, 0, tzinfo=UTC),
        ended_at=datetime(2026, 2, 25, 10, 1, 0, tzinfo=UTC),
        tool_calls=[
            ToolCallRecord(
                tool_name="read",
                args_hash="h",
                result_status="ok",
                duration_ms=10.0,
            ),
        ],
    )


def _make_candidate(
    text: str,
    candidate_id: str = "c1",
    parent_id: str | None = None,
    generation: int = 0,
    token_count: int = 0,
) -> Candidate:
    return Candidate(
        id=candidate_id,
        text=text,
        parent_id=parent_id,
        generation=generation,
        token_count=token_count or len(text.split()),
    )


SEED_TEXT = """\
## SKILL INTENT [IMMUTABLE]
Plan business travel efficiently.

## Steps
1. Check calendar
2. Book flights
3. Confirm hotel
"""

SEED_TOKEN_COUNT = len(SEED_TEXT.split())


class TestCheckEligible:
    """C1: min traces, cooloff, exempt tags, generation limit."""

    def test_eligible_with_enough_traces(self) -> None:
        config = SkillImproverConfig(min_traces=5)
        g = Guardrails(config)
        traces = [_make_trace(turn=i) for i in range(10)]
        assert g.check_eligible("test-skill", traces) is True

    def test_not_eligible_too_few_traces(self) -> None:
        config = SkillImproverConfig(min_traces=30)
        g = Guardrails(config)
        traces = [_make_trace(turn=i) for i in range(10)]
        assert g.check_eligible("test-skill", traces) is False

    def test_not_eligible_in_cooloff(self) -> None:
        config = SkillImproverConfig(min_traces=5, cooloff_turns=200)
        g = Guardrails(config)
        g.set_cooloff("test-skill", until_turn=999)
        traces = [_make_trace(turn=i) for i in range(10)]
        assert g.check_eligible("test-skill", traces, current_turn=50) is False

    def test_eligible_after_cooloff_expires(self) -> None:
        config = SkillImproverConfig(min_traces=5)
        g = Guardrails(config)
        g.set_cooloff("test-skill", until_turn=100)
        traces = [_make_trace(turn=i) for i in range(10)]
        assert g.check_eligible("test-skill", traces, current_turn=150) is True

    def test_not_eligible_exempt_tag(self) -> None:
        config = SkillImproverConfig(
            min_traces=5,
            exempt_tags=["security-critical"],
        )
        g = Guardrails(config)
        traces = [_make_trace(turn=i) for i in range(10)]
        assert (
            g.check_eligible(
                "test-skill",
                traces,
                skill_tags=["security-critical"],
            )
            is False
        )

    def test_eligible_non_exempt_tag(self) -> None:
        config = SkillImproverConfig(min_traces=5, exempt_tags=["auth"])
        g = Guardrails(config)
        traces = [_make_trace(turn=i) for i in range(10)]
        assert (
            g.check_eligible(
                "test-skill",
                traces,
                skill_tags=["utility"],
            )
            is True
        )

    def test_not_eligible_generation_limit(self) -> None:
        config = SkillImproverConfig(min_traces=5, max_generations=10)
        g = Guardrails(config)
        g.set_generation("test-skill", 10)
        traces = [_make_trace(turn=i) for i in range(10)]
        assert g.check_eligible("test-skill", traces) is False

    def test_eligible_under_generation_limit(self) -> None:
        config = SkillImproverConfig(min_traces=5, max_generations=10)
        g = Guardrails(config)
        g.set_generation("test-skill", 5)
        traces = [_make_trace(turn=i) for i in range(10)]
        assert g.check_eligible("test-skill", traces) is True


class TestValidateCandidate:
    """C2: intent preserved, token budget, anchor distance, oscillation."""

    def test_valid_candidate_passes(self) -> None:
        config = SkillImproverConfig(max_token_ratio=1.5, anchor_distance_threshold=0.15)
        g = Guardrails(config)
        seed = _make_candidate(SEED_TEXT, candidate_id="seed", token_count=SEED_TOKEN_COUNT)
        # Candidate with minor changes
        candidate_text = SEED_TEXT.replace("Check calendar", "Review calendar availability")
        candidate = _make_candidate(
            candidate_text,
            candidate_id="c1",
            parent_id="seed",
            token_count=len(candidate_text.split()),
        )
        assert g.validate_candidate(candidate, seed) is True

    def test_rejected_intent_modified(self) -> None:
        config = SkillImproverConfig()
        g = Guardrails(config)
        seed = _make_candidate(SEED_TEXT, candidate_id="seed", token_count=SEED_TOKEN_COUNT)
        # Change the immutable intent
        bad_text = SEED_TEXT.replace(
            "Plan business travel efficiently.",
            "Plan personal vacations.",
        )
        candidate = _make_candidate(
            bad_text,
            candidate_id="c1",
            parent_id="seed",
            token_count=len(bad_text.split()),
        )
        assert g.validate_candidate(candidate, seed) is False

    def test_rejected_token_budget_exceeded(self) -> None:
        config = SkillImproverConfig(max_token_ratio=1.5)
        g = Guardrails(config)
        seed = _make_candidate(SEED_TEXT, candidate_id="seed", token_count=10)
        # Way over budget
        candidate = _make_candidate(
            SEED_TEXT + "\n" * 100 + "extra " * 100,
            candidate_id="c1",
            parent_id="seed",
            token_count=200,
        )
        assert g.validate_candidate(candidate, seed) is False

    def test_rejected_anchor_distance_exceeded(self) -> None:
        config = SkillImproverConfig(anchor_distance_threshold=0.15)
        g = Guardrails(config)
        seed = _make_candidate(SEED_TEXT, candidate_id="seed", token_count=SEED_TOKEN_COUNT)
        # Completely different text
        totally_different = (
            "## SKILL INTENT [IMMUTABLE]\n"
            "Plan business travel efficiently.\n\n"
            "## Procedure\n"
            "Alpha bravo charlie delta echo foxtrot golf hotel india.\n"
            "Juliet kilo lima mike november oscar papa quebec romeo.\n"
            "Sierra tango uniform victor whiskey xray yankee zulu.\n"
        )
        candidate = _make_candidate(
            totally_different,
            candidate_id="c1",
            parent_id="seed",
            token_count=len(totally_different.split()),
        )
        assert g.validate_candidate(candidate, seed) is False


class TestIntentParsing:
    """C3: Intent header extraction and comparison."""

    def test_extract_intent(self) -> None:
        config = SkillImproverConfig()
        g = Guardrails(config)
        intent = g.extract_intent(SEED_TEXT)
        assert "Plan business travel efficiently." in intent

    def test_extract_intent_no_header(self) -> None:
        config = SkillImproverConfig()
        g = Guardrails(config)
        intent = g.extract_intent("# Just a skill\nDo stuff.")
        assert intent == ""

    def test_intent_comparison_same(self) -> None:
        config = SkillImproverConfig()
        g = Guardrails(config)
        assert g.intent_preserved(SEED_TEXT, SEED_TEXT) is True

    def test_intent_comparison_modified(self) -> None:
        config = SkillImproverConfig()
        g = Guardrails(config)
        modified = SEED_TEXT.replace(
            "Plan business travel efficiently.",
            "Something completely different.",
        )
        assert g.intent_preserved(modified, SEED_TEXT) is False


class TestAnchorDistance:
    """C4: SequenceMatcher ratio as proxy for semantic distance."""

    def test_identical_text_zero_distance(self) -> None:
        config = SkillImproverConfig()
        g = Guardrails(config)
        assert g.anchor_distance(SEED_TEXT, SEED_TEXT) == 0.0

    def test_similar_text_small_distance(self) -> None:
        config = SkillImproverConfig()
        g = Guardrails(config)
        modified = SEED_TEXT.replace("Check calendar", "Review calendar schedule")
        dist = g.anchor_distance(modified, SEED_TEXT)
        assert 0.0 < dist < 0.15

    def test_very_different_text_large_distance(self) -> None:
        config = SkillImproverConfig()
        g = Guardrails(config)
        different = "Alpha bravo charlie delta echo foxtrot golf hotel india."
        dist = g.anchor_distance(different, SEED_TEXT)
        assert dist > 0.5


class TestOscillationDetection:
    """C5: Fingerprint comparison against recent versions."""

    def test_no_oscillation_new_candidate(self) -> None:
        config = SkillImproverConfig()
        g = Guardrails(config)
        candidate = _make_candidate("New unique text version A")
        recent = [_make_candidate(f"Version {i}") for i in range(5)]
        assert g.is_oscillation(candidate, recent) is False

    def test_oscillation_detected_exact_match(self) -> None:
        config = SkillImproverConfig()
        g = Guardrails(config)
        candidate = _make_candidate("Repeated text version")
        recent = [
            _make_candidate("Version A"),
            _make_candidate("Repeated text version"),  # Same fingerprint
            _make_candidate("Version C"),
        ]
        assert g.is_oscillation(candidate, recent) is True

    def test_oscillation_detected_near_match(self) -> None:
        config = SkillImproverConfig()
        g = Guardrails(config)
        candidate = _make_candidate("Repeated text version here")
        recent = [
            _make_candidate("Repeated text version here!"),  # Very similar
        ]
        # Near-match detection depends on cosine threshold (0.05)
        assert g.is_oscillation(candidate, recent) is True


class TestCooloffManagement:
    """C6: Set cooloff, check cooloff, expire cooloff."""

    def test_set_and_check_cooloff(self) -> None:
        config = SkillImproverConfig()
        g = Guardrails(config)
        g.set_cooloff("skill-a", until_turn=100)
        assert g.in_cooloff("skill-a", current_turn=50) is True

    def test_cooloff_expired(self) -> None:
        config = SkillImproverConfig()
        g = Guardrails(config)
        g.set_cooloff("skill-a", until_turn=100)
        assert g.in_cooloff("skill-a", current_turn=150) is False

    def test_no_cooloff_by_default(self) -> None:
        config = SkillImproverConfig()
        g = Guardrails(config)
        assert g.in_cooloff("skill-a", current_turn=0) is False

    def test_cooloff_per_skill(self) -> None:
        config = SkillImproverConfig()
        g = Guardrails(config)
        g.set_cooloff("skill-a", until_turn=100)
        assert g.in_cooloff("skill-a", current_turn=50) is True
        assert g.in_cooloff("skill-b", current_turn=50) is False
