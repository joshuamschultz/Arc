"""Tests for arcagent.orchestration.prompts — spawn_task guidance."""

from __future__ import annotations

from arcagent.orchestration.prompts import SPAWN_GUIDANCE


class TestSpawnGuidance:
    """SPAWN_GUIDANCE describes when and how to use spawn_task."""

    def test_is_string(self) -> None:
        assert isinstance(SPAWN_GUIDANCE, str)
        assert len(SPAWN_GUIDANCE) > 100

    def test_contains_decision_gate(self) -> None:
        """Guidance includes when-to and when-not-to criteria."""
        assert "Use spawn_task when:" in SPAWN_GUIDANCE
        assert "Do NOT use spawn_task when:" in SPAWN_GUIDANCE

    def test_contains_example(self) -> None:
        """Guidance includes a few-shot delegation example."""
        assert "<example>" in SPAWN_GUIDANCE
        assert "Good delegation:" in SPAWN_GUIDANCE
        assert "Bad delegation:" in SPAWN_GUIDANCE

    def test_decision_oriented(self) -> None:
        """Guidance reasons about WHEN to use, not just HOW."""
        assert "evaluate" in SPAWN_GUIDANCE.lower() or "when" in SPAWN_GUIDANCE.lower()

    def test_exported_from_orchestration(self) -> None:
        """SPAWN_GUIDANCE is exported at the orchestration package level."""
        from arcagent.orchestration import SPAWN_GUIDANCE as exported

        assert exported is SPAWN_GUIDANCE
