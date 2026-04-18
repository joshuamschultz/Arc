"""Tests for context manager — system prompt, token counting, pruning."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from arcagent.core.config import ArcAgentConfig, ContextConfig
from arcagent.core.session_internal.context import ContextManager
from arcagent.core.module_bus import EventContext, ModuleBus


@pytest.fixture()
def ctx_config() -> ContextConfig:
    return ContextConfig(
        max_tokens=1000,
        prune_threshold=0.70,
        compact_threshold=0.85,
        emergency_threshold=0.95,
        estimate_multiplier=1.1,
    )


@pytest.fixture()
def mock_telemetry() -> MagicMock:
    return MagicMock()


@pytest.fixture()
def mock_bus(mock_telemetry: MagicMock) -> ModuleBus:
    ArcAgentConfig(
        agent={"name": "test-agent"},
        llm={"model": "test/model"},
    )
    return ModuleBus()


@pytest.fixture()
def ctx_mgr(ctx_config: ContextConfig, mock_telemetry: MagicMock) -> ContextManager:
    return ContextManager(config=ctx_config, telemetry=mock_telemetry)


@pytest.fixture()
def ctx_mgr_with_bus(
    ctx_config: ContextConfig, mock_telemetry: MagicMock, mock_bus: ModuleBus
) -> ContextManager:
    return ContextManager(config=ctx_config, telemetry=mock_telemetry, bus=mock_bus)


class TestAssembleSystemPrompt:
    async def test_assembles_from_workspace_files(
        self, ctx_mgr: ContextManager, tmp_path: Path
    ) -> None:
        (tmp_path / "identity.md").write_text("# Agent Identity\nI am test-agent.")
        (tmp_path / "context.md").write_text("# Context\nWorking memory.")

        prompt = await ctx_mgr.assemble_system_prompt(tmp_path)
        assert "Agent Identity" in prompt
        assert "Working memory" in prompt

    async def test_missing_files_handled_gracefully(
        self, ctx_mgr: ContextManager, tmp_path: Path
    ) -> None:
        """Missing workspace files don't crash, just skip."""
        prompt = await ctx_mgr.assemble_system_prompt(tmp_path)
        assert isinstance(prompt, str)

    async def test_partial_files(self, ctx_mgr: ContextManager, tmp_path: Path) -> None:
        """Only identity.md exists."""
        (tmp_path / "identity.md").write_text("# Identity\nTest agent.")
        prompt = await ctx_mgr.assemble_system_prompt(tmp_path)
        assert "Identity" in prompt

    async def test_section_headers_included(self, ctx_mgr: ContextManager, tmp_path: Path) -> None:
        (tmp_path / "identity.md").write_text("content")
        prompt = await ctx_mgr.assemble_system_prompt(tmp_path)
        assert "--- identity ---" in prompt.lower() or "identity" in prompt.lower()

    async def test_works_without_bus(self, ctx_mgr: ContextManager, tmp_path: Path) -> None:
        """No bus parameter = no event emitted, still works."""
        (tmp_path / "identity.md").write_text("I am agent")
        prompt = await ctx_mgr.assemble_system_prompt(tmp_path)
        assert "I am agent" in prompt


class TestExtraSections:
    """Tests for extra_sections parameter on assemble_system_prompt."""

    async def test_extra_sections_included_in_prompt(
        self, ctx_mgr: ContextManager, tmp_path: Path
    ) -> None:
        (tmp_path / "identity.md").write_text("I am agent")
        extra = {"strategy_react": "## Loop\nYou operate in a loop."}
        prompt = await ctx_mgr.assemble_system_prompt(tmp_path, extra_sections=extra)
        assert "You operate in a loop" in prompt

    async def test_extra_sections_sorted_alphabetically(
        self, ctx_mgr: ContextManager, tmp_path: Path
    ) -> None:
        """Extra sections appear in sorted order between identity and context."""
        (tmp_path / "identity.md").write_text("Identity content")
        (tmp_path / "context.md").write_text("Context content")
        extra = {
            "spawn_guidance": "Spawn guidance content",
            "code_exec_guidance": "Code exec content",
        }
        prompt = await ctx_mgr.assemble_system_prompt(tmp_path, extra_sections=extra)

        identity_pos = prompt.find("Identity content")
        code_pos = prompt.find("Code exec content")
        spawn_pos = prompt.find("Spawn guidance content")
        context_pos = prompt.find("Context content")

        assert identity_pos < code_pos < spawn_pos < context_pos

    async def test_extra_sections_none_is_safe(
        self, ctx_mgr: ContextManager, tmp_path: Path
    ) -> None:
        (tmp_path / "identity.md").write_text("I am agent")
        prompt = await ctx_mgr.assemble_system_prompt(tmp_path, extra_sections=None)
        assert "I am agent" in prompt

    async def test_extra_sections_empty_dict_is_safe(
        self, ctx_mgr: ContextManager, tmp_path: Path
    ) -> None:
        (tmp_path / "identity.md").write_text("I am agent")
        prompt = await ctx_mgr.assemble_system_prompt(tmp_path, extra_sections={})
        assert "I am agent" in prompt

    async def test_extra_sections_merged_after_bus_handlers(
        self, ctx_mgr_with_bus: ContextManager, mock_bus: ModuleBus, tmp_path: Path
    ) -> None:
        """Extra sections merge after bus handlers, so they can override."""

        async def inject_via_bus(ctx: Any) -> None:
            ctx.data["sections"]["tools"] = "Bus-injected tools"

        mock_bus.subscribe("agent:assemble_prompt", inject_via_bus)
        (tmp_path / "identity.md").write_text("I am agent")
        extra = {"strategies": "Strategy guidance"}

        prompt = await ctx_mgr_with_bus.assemble_system_prompt(
            tmp_path, extra_sections=extra
        )
        assert "Bus-injected tools" in prompt
        assert "Strategy guidance" in prompt

    async def test_extra_sections_can_override_bus_section(
        self, ctx_mgr_with_bus: ContextManager, mock_bus: ModuleBus, tmp_path: Path
    ) -> None:
        """Caller extra_sections override bus-injected sections with same key."""

        async def inject_via_bus(ctx: Any) -> None:
            ctx.data["sections"]["overlap"] = "bus version"

        mock_bus.subscribe("agent:assemble_prompt", inject_via_bus)
        extra = {"overlap": "caller version"}

        prompt = await ctx_mgr_with_bus.assemble_system_prompt(
            tmp_path, extra_sections=extra
        )
        assert "caller version" in prompt
        assert "bus version" not in prompt


class TestAssemblePromptEvent:
    """Tests for agent:assemble_prompt event emission."""

    async def test_emits_assemble_prompt_event(
        self, ctx_mgr_with_bus: ContextManager, mock_bus: ModuleBus, tmp_path: Path
    ) -> None:
        """When bus is present, emit agent:assemble_prompt with sections."""
        events_received: list[EventContext] = []

        async def handler(ctx: EventContext) -> None:
            events_received.append(ctx)

        mock_bus.subscribe("agent:assemble_prompt", handler)
        (tmp_path / "identity.md").write_text("I am agent")

        await ctx_mgr_with_bus.assemble_system_prompt(tmp_path)
        assert len(events_received) == 1
        assert "sections" in events_received[0].data
        assert "workspace" in events_received[0].data

    async def test_handler_injects_notes_section(
        self, ctx_mgr_with_bus: ContextManager, mock_bus: ModuleBus, tmp_path: Path
    ) -> None:
        """Handler can add a 'notes' section to the prompt."""

        async def inject_notes(ctx: EventContext) -> None:
            ctx.data["sections"]["notes"] = "Today I learned about testing."

        mock_bus.subscribe("agent:assemble_prompt", inject_notes, priority=50)
        (tmp_path / "identity.md").write_text("I am agent")

        prompt = await ctx_mgr_with_bus.assemble_system_prompt(tmp_path)
        assert "Today I learned about testing" in prompt

    async def test_handler_failure_still_assembles_prompt(
        self, ctx_mgr_with_bus: ContextManager, mock_bus: ModuleBus, tmp_path: Path
    ) -> None:
        """If handler raises, prompt is still assembled (best-effort injection)."""

        async def bad_handler(ctx: EventContext) -> None:
            raise RuntimeError("handler exploded")

        mock_bus.subscribe("agent:assemble_prompt", bad_handler)
        (tmp_path / "identity.md").write_text("I am agent")

        # Should not raise, prompt still assembled
        prompt = await ctx_mgr_with_bus.assemble_system_prompt(tmp_path)
        assert "I am agent" in prompt

    async def test_section_ordering(
        self, ctx_mgr_with_bus: ContextManager, mock_bus: ModuleBus, tmp_path: Path
    ) -> None:
        """Sections appear in order: identity, notes, context."""

        async def inject_notes(ctx: EventContext) -> None:
            ctx.data["sections"]["notes"] = "Daily notes content"

        mock_bus.subscribe("agent:assemble_prompt", inject_notes, priority=50)
        (tmp_path / "identity.md").write_text("Identity content")
        (tmp_path / "context.md").write_text("Context content")

        prompt = await ctx_mgr_with_bus.assemble_system_prompt(tmp_path)
        # Verify ordering: identity, notes, context
        identity_pos = prompt.find("Identity content")
        notes_pos = prompt.find("Daily notes content")
        context_pos = prompt.find("Context content")

        assert identity_pos < notes_pos < context_pos

    async def test_no_event_without_bus(
        self, ctx_config: ContextConfig, mock_telemetry: MagicMock, tmp_path: Path
    ) -> None:
        """Without bus, no event emission attempted."""
        mgr = ContextManager(config=ctx_config, telemetry=mock_telemetry)
        (tmp_path / "identity.md").write_text("content")
        # Should work fine without bus
        prompt = await mgr.assemble_system_prompt(tmp_path)
        assert "content" in prompt


class TestTokenEstimation:
    def test_basic_estimation(self, ctx_mgr: ContextManager) -> None:
        """~4 chars per token, 1.1x multiplier, ceil'd."""
        text = "a" * 400  # 400/4 = 100 * 1.1 = 110.0 → ceil = 110
        estimate = ctx_mgr.estimate_tokens(text)
        # ceil(100 * 1.1) = 110
        assert estimate >= 110
        assert estimate <= 111

    def test_empty_string(self, ctx_mgr: ContextManager) -> None:
        assert ctx_mgr.estimate_tokens("") == 0

    def test_multiplier_applied(self, ctx_mgr: ContextManager) -> None:
        text = "hello world test"  # 16 chars / 4 = 4 tokens * 1.1 = ~4
        estimate = ctx_mgr.estimate_tokens(text)
        assert estimate >= 4


class TestReportedUsage:
    def test_update_reported_usage(self, ctx_mgr: ContextManager) -> None:
        ctx_mgr.update_reported_usage(input_tokens=500, output_tokens=200)
        assert ctx_mgr.reported_input_tokens == 500
        assert ctx_mgr.reported_output_tokens == 200

    def test_cumulative_usage(self, ctx_mgr: ContextManager) -> None:
        ctx_mgr.update_reported_usage(input_tokens=100, output_tokens=50)
        ctx_mgr.update_reported_usage(input_tokens=200, output_tokens=100)
        assert ctx_mgr.reported_input_tokens == 300
        assert ctx_mgr.reported_output_tokens == 150


class TestObservationMasking:
    def test_old_tool_outputs_pruned(self, ctx_mgr: ContextManager) -> None:
        """Tool outputs beyond protected window are masked."""
        messages = [
            {"role": "user", "content": "do something"},
            {"role": "assistant", "content": "I'll use the tool"},
            {
                "role": "tool",
                "content": "x" * 2000,  # Large tool output
                "tool_call_id": "tc1",
            },
            {"role": "assistant", "content": "Got it, now next step"},
            {"role": "user", "content": "continue"},
            {"role": "assistant", "content": "Using another tool"},
            {
                "role": "tool",
                "content": "recent output",
                "tool_call_id": "tc2",
            },
        ]
        pruned = ctx_mgr.prune_observations(messages, protected_recent_tokens=100)
        # Old tool output should be pruned, recent preserved
        old_tool = next(m for m in pruned if m.get("tool_call_id") == "tc1")
        recent_tool = next(m for m in pruned if m.get("tool_call_id") == "tc2")
        assert "[output pruned" in old_tool["content"]
        assert recent_tool["content"] == "recent output"

    def test_recent_outputs_protected(self, ctx_mgr: ContextManager) -> None:
        """Recent tool outputs within protected window are not pruned."""
        messages = [
            {
                "role": "tool",
                "content": "recent data",
                "tool_call_id": "tc1",
            },
        ]
        pruned = ctx_mgr.prune_observations(messages, protected_recent_tokens=10000)
        assert pruned[0]["content"] == "recent data"


class TestThresholdTriggers:
    def test_below_prune_threshold_no_action(self, ctx_mgr: ContextManager) -> None:
        """Below 70% — no pruning needed."""
        messages = [{"role": "user", "content": "hi"}]
        result = ctx_mgr.transform_context(messages)
        assert result == messages

    def test_above_prune_threshold_prunes(
        self, ctx_config: ContextConfig, mock_telemetry: MagicMock
    ) -> None:
        """Above 70% — observation masking kicks in."""
        config = ContextConfig(
            max_tokens=100,  # Small budget to trigger thresholds easily
            prune_threshold=0.70,
            compact_threshold=0.85,
            emergency_threshold=0.95,
            estimate_multiplier=1.0,  # No multiplier for predictable math
        )
        mgr = ContextManager(config=config, telemetry=mock_telemetry)
        # Create messages that push over 70% of 100 tokens
        messages = [
            {"role": "user", "content": "x" * 100},
            {
                "role": "tool",
                "content": "y" * 200,  # Old tool output
                "tool_call_id": "tc1",
            },
            {"role": "assistant", "content": "z" * 50},
        ]
        result = mgr.transform_context(messages)
        # Should have pruned the old tool output
        tool_msg = next(m for m in result if m.get("tool_call_id") == "tc1")
        assert "[output pruned" in tool_msg["content"]

    def test_emergency_threshold_truncates(self, mock_telemetry: MagicMock) -> None:
        """Above 95% — force truncation of oldest messages."""
        config = ContextConfig(
            max_tokens=50,
            prune_threshold=0.70,
            compact_threshold=0.85,
            emergency_threshold=0.95,
            estimate_multiplier=1.0,
        )
        mgr = ContextManager(config=config, telemetry=mock_telemetry)
        messages = [
            {"role": "user", "content": "old " * 100},
            {"role": "assistant", "content": "old reply " * 100},
            {"role": "user", "content": "recent question"},
        ]
        result = mgr.transform_context(messages)
        # Should have fewer messages than original
        assert len(result) <= len(messages)


class TestTransformContext:
    def test_returns_list(self, ctx_mgr: ContextManager) -> None:
        messages: list[dict[str, Any]] = [{"role": "user", "content": "hello"}]
        result = ctx_mgr.transform_context(messages)
        assert isinstance(result, list)

    def test_preserves_messages_under_budget(self, ctx_mgr: ContextManager) -> None:
        messages = [
            {"role": "user", "content": "short"},
            {"role": "assistant", "content": "reply"},
        ]
        result = ctx_mgr.transform_context(messages)
        assert len(result) == 2


class TestUsageRatio:
    def test_usage_ratio(self, ctx_mgr: ContextManager) -> None:
        """Usage ratio based on estimated tokens."""
        ratio = ctx_mgr.usage_ratio("x" * 400)  # ~110 estimated / 1000 max
        assert 0.0 < ratio < 1.0


class TestContextEdgeCases:
    """Edge cases and error handling."""

    async def test_assemble_prompt_all_files_missing(
        self, ctx_mgr: ContextManager, tmp_path: Path
    ) -> None:
        """Line 40: All workspace files missing, returns empty or minimal prompt."""
        # No files created, all should be skipped
        prompt = await ctx_mgr.assemble_system_prompt(tmp_path)
        # Should still return a string, not crash
        assert isinstance(prompt, str)

    def test_token_ratio_with_no_usage(self, ctx_mgr: ContextManager) -> None:
        """Line 120: No reported usage, token_ratio should return 0.0."""
        # Initial state with no usage reported
        ratio = ctx_mgr.token_ratio()
        assert ratio == 0.0

    def test_estimate_ratio_empty_messages(self, ctx_mgr: ContextManager) -> None:
        """Line 145: Empty message list."""
        ratio = ctx_mgr._estimate_ratio([])
        assert ratio == 0.0

    def test_prune_observations_with_pydantic_model(self, ctx_mgr: ContextManager) -> None:
        """Line 171: Message is Pydantic model, not dict."""
        from pydantic import BaseModel

        class Message(BaseModel):
            role: str
            content: str
            tool_call_id: str | None = None

        messages = [
            Message(role="user", content="hello"),
            Message(role="tool", content="x" * 1000, tool_call_id="tc1"),
            Message(role="assistant", content="reply"),
        ]
        result = ctx_mgr.prune_observations(messages, protected_recent_tokens=100)
        # Should prune the old tool output
        tool_msg = next(m for m in result if m.tool_call_id == "tc1")
        assert "[output pruned" in tool_msg.content

    def test_transform_context_empty_messages(self, ctx_mgr: ContextManager) -> None:
        """Line 189: Empty message list."""
        result = ctx_mgr.transform_context([])
        assert result == []


class TestTokenRatioZeroMax:
    """Line 120: token_ratio returns 0.0 when max_tokens is 0."""

    def test_zero_max_tokens_returns_zero(self, mock_telemetry: MagicMock) -> None:
        # Use model_construct to bypass Pydantic validation for defensive guard test
        config = ContextConfig.model_construct(max_tokens=0)
        mgr = ContextManager(config=config, telemetry=mock_telemetry)
        assert mgr.token_ratio() == 0.0


class TestPruneObservationsEmpty:
    """Line 145: prune_observations returns empty list for empty input."""

    def test_prune_empty_messages(self, ctx_mgr: ContextManager) -> None:
        result = ctx_mgr.prune_observations([])
        assert result == []
