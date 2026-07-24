"""Tests for the externalized spawn_task guidance prompt."""

from __future__ import annotations

from arcprompt import load_stock


class TestSpawnGuidance:
    """The spawn_guidance stock prompt describes when and how to use spawn_task."""

    def test_is_string(self) -> None:
        guidance = load_stock("arcagent", "spawn_guidance")
        assert isinstance(guidance, str)
        assert len(guidance) > 100

    def test_contains_decision_gate(self) -> None:
        """Guidance includes when-to and when-not-to criteria."""
        guidance = load_stock("arcagent", "spawn_guidance")
        assert "Use spawn_task when:" in guidance
        assert "Do NOT use spawn_task when:" in guidance

    def test_contains_example(self) -> None:
        """Guidance includes a few-shot delegation example."""
        guidance = load_stock("arcagent", "spawn_guidance")
        assert "<example>" in guidance
        assert "Good delegation:" in guidance
        assert "Bad delegation:" in guidance

    def test_decision_oriented(self) -> None:
        """Guidance reasons about WHEN to use, not just HOW."""
        guidance = load_stock("arcagent", "spawn_guidance")
        assert "evaluate" in guidance.lower() or "when" in guidance.lower()
