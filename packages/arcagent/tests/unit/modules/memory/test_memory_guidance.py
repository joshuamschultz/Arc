"""Tests for memory guidance injection via assemble_prompt."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from arcagent.core.config import EvalConfig, MemoryConfig
from arcagent.core.module_bus import EventContext
from arcagent.modules.memory.markdown_memory import MarkdownMemoryModule


def _make_telemetry() -> MagicMock:
    t = MagicMock()
    t.audit_event = MagicMock()
    return t


def _make_module(workspace: Path) -> MarkdownMemoryModule:
    return MarkdownMemoryModule(
        config=MemoryConfig(),
        eval_config=EvalConfig(),
        telemetry=_make_telemetry(),
        workspace=workspace,
    )


def _make_ctx(event: str, data: dict[str, Any] | None = None) -> EventContext:
    return EventContext(
        event=event,
        data=data or {},
        agent_did="did:arc:test",
        trace_id="trace-1",
    )


class TestMemoryGuidanceInjection:
    """T4.1: Memory guidance injection."""

    async def test_injects_guidance_when_no_memory_section(self, tmp_path: Path) -> None:
        """Guidance injected when identity.md has no ## Memory section."""
        module = _make_module(tmp_path)
        sections: dict[str, str] = {"identity": "# Agent Identity\n\nI am an agent."}
        ctx = _make_ctx("agent:assemble_prompt", {"sections": sections})

        await module._on_assemble_prompt(ctx)

        assert "memory_guidance" in sections
        assert "memory_search" in sections["memory_guidance"]

    async def test_no_guidance_when_identity_has_memory_section(self, tmp_path: Path) -> None:
        """Guidance NOT injected when identity.md already has ## Memory."""
        module = _make_module(tmp_path)
        sections: dict[str, str] = {
            "identity": "# Agent Identity\n\n## Memory\n\nI manage my own memory."
        }
        ctx = _make_ctx("agent:assemble_prompt", {"sections": sections})

        await module._on_assemble_prompt(ctx)

        assert "memory_guidance" not in sections

    async def test_default_guidance_content(self, tmp_path: Path) -> None:
        """Default guidance text mentions key memory capabilities."""
        module = _make_module(tmp_path)
        guidance = module._default_memory_guidance()
        assert "memory_search" in guidance
        assert "Memory" in guidance
