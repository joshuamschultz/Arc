"""SPEC-021 task 3.1 — memory module decorator-form tests.

The new ``capabilities.py`` exposes:

  * 6 ``@hook`` functions (assemble_prompt, pre_tool, post_tool,
    post_respond, pre_compaction, shutdown).
  * 1 ``@tool`` (``memory_search``).
  * 1 ``@background_task`` (``entity_extraction_loop``).

This file verifies:

  1. All 8 capabilities register via :class:`CapabilityLoader` at the
     correct event/name/kind/priority.
  2. The hook priorities match the legacy
     :class:`MarkdownMemoryModule.startup` registrations.
  3. ``inject_memory_sections`` writes the recent-notes section when a
     today's-notes file exists.
  4. ``memory_search`` returns the no-results sentinel when the
     hybrid-search backend is empty.

Legacy ``MarkdownMemoryModule`` tests in ``test_markdown_memory.py`` /
``test_memory_wiring.py`` continue to verify behaviour at the wrapper
level.
"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcagent.core.capability_loader import CapabilityLoader
from arcagent.core.capability_registry import CapabilityRegistry
from arcagent.core.config import EvalConfig
from arcagent.modules.memory import _runtime


@pytest.fixture(autouse=True)
def _reset_runtime() -> None:
    _runtime.reset()


@pytest.fixture
def configured(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _runtime.configure(
        workspace=workspace,
        eval_config=EvalConfig(),
        telemetry=MagicMock(),
        agent_name="test",
    )
    return workspace


@pytest.mark.asyncio
class TestLoaderRegistration:
    async def test_all_capabilities_register(self, tmp_path: Path) -> None:
        from arcagent.modules.memory import capabilities as memory_caps

        # The loader scans .py files in the directory; we want to load
        # only capabilities.py without also scanning sibling modules
        # (which would pull in unrelated decorator-stamped artefacts in
        # future and slow the test). Point the loader at a directory
        # that contains a single symlink/copy of capabilities.py.
        scan_dir = tmp_path / "memory_caps"
        scan_dir.mkdir()
        target = scan_dir / "capabilities.py"
        target.write_text(Path(memory_caps.__file__).read_text(encoding="utf-8"))

        reg = CapabilityRegistry()
        loader = CapabilityLoader(scan_roots=[("memory", scan_dir)], registry=reg)
        # Background-task registration spawns the task; cancel
        # immediately by inspecting the registered entry.
        await loader.scan_and_register()

        # Hooks
        prompt_hooks = await reg.get_hooks("agent:assemble_prompt")
        pre_tool_hooks = await reg.get_hooks("agent:pre_tool")
        post_tool_hooks = await reg.get_hooks("agent:post_tool")
        post_respond_hooks = await reg.get_hooks("agent:post_respond")
        pre_compact_hooks = await reg.get_hooks("agent:pre_compaction")
        shutdown_hooks = await reg.get_hooks("agent:shutdown")

        assert any(h.meta.name == "inject_memory_sections" for h in prompt_hooks)
        assert any(h.meta.name == "memory_pre_tool" for h in pre_tool_hooks)
        assert any(h.meta.name == "memory_post_tool" for h in post_tool_hooks)
        assert any(h.meta.name == "memory_post_respond" for h in post_respond_hooks)
        assert any(h.meta.name == "memory_pre_compaction" for h in pre_compact_hooks)
        assert any(h.meta.name == "memory_shutdown" for h in shutdown_hooks)

        # Tool
        tool_entry = await reg.get_tool("memory_search")
        assert tool_entry is not None
        assert tool_entry.meta.classification == "read_only"

        # Background task — drain so the test doesn't leak a runner.
        task_entry = await reg.get_task("entity_extraction_loop")
        assert task_entry is not None
        assert task_entry.meta.interval == pytest.approx(1.0)
        if task_entry.task is not None:
            task_entry.task.cancel()
            try:
                await task_entry.task
            except asyncio.CancelledError:
                pass

    async def test_hook_priorities_match_legacy(self) -> None:
        from arcagent.modules.memory.capabilities import (
            inject_memory_sections,
            memory_post_respond,
            memory_post_tool,
            memory_pre_compaction,
            memory_pre_tool,
            memory_shutdown,
        )

        # Priorities mirror MarkdownMemoryModule.startup() registrations.
        assert inject_memory_sections._arc_capability_meta.priority == 50  # type: ignore[attr-defined]
        assert memory_pre_tool._arc_capability_meta.priority == 10  # type: ignore[attr-defined]
        assert memory_post_tool._arc_capability_meta.priority == 100  # type: ignore[attr-defined]
        assert memory_post_respond._arc_capability_meta.priority == 100  # type: ignore[attr-defined]
        assert memory_pre_compaction._arc_capability_meta.priority == 50  # type: ignore[attr-defined]
        assert memory_shutdown._arc_capability_meta.priority == 100  # type: ignore[attr-defined]


@pytest.mark.asyncio
class TestInjectMemorySections:
    async def test_writes_notes_section_when_file_present(self, configured: Path) -> None:
        from arcagent.modules.memory.capabilities import inject_memory_sections

        notes_dir = configured / "notes"
        notes_dir.mkdir()
        today = date.today().isoformat()
        (notes_dir / f"{today}.md").write_text("today's notes content")

        sections: dict[str, str] = {}
        ctx = SimpleNamespace(data={"sections": sections})
        await inject_memory_sections(ctx)

        assert "notes" in sections
        assert "today's notes content" in sections["notes"]
        # Default guidance also injected when identity has no Memory section.
        assert "memory_guidance" in sections

    async def test_skips_guidance_when_identity_overrides(self, configured: Path) -> None:
        from arcagent.modules.memory.capabilities import inject_memory_sections

        sections: dict[str, str] = {"identity": "## Memory\nCustom"}
        ctx = SimpleNamespace(data={"sections": sections})
        await inject_memory_sections(ctx)
        assert "memory_guidance" not in sections


@pytest.mark.asyncio
class TestMemorySearchTool:
    async def test_no_results_returns_sentinel(self, configured: Path) -> None:
        from arcagent.modules.memory import capabilities as memory_caps

        st = _runtime.state()
        st.hybrid_search.search = AsyncMock(return_value=[])  # type: ignore[method-assign]

        result = await memory_caps.memory_search(query="anything")
        assert result == "No memory results found."

    async def test_results_wrapped_in_boundary_markers(self, configured: Path) -> None:
        from arcagent.modules.memory import capabilities as memory_caps

        st = _runtime.state()
        fake_result = SimpleNamespace(source="notes/2026-01-01.md", score=0.92, content="fact A")
        st.hybrid_search.search = AsyncMock(return_value=[fake_result])  # type: ignore[method-assign]

        result: Any = await memory_caps.memory_search(query="fact")
        assert "<memory-result" in result
        assert "fact A" in result
        assert "</memory-result>" in result


@pytest.mark.asyncio
class TestRuntimeContract:
    async def test_unconfigured_raises(self) -> None:
        from arcagent.modules.memory.capabilities import inject_memory_sections

        with pytest.raises(RuntimeError, match="before runtime is configured"):
            await inject_memory_sections(SimpleNamespace(data={"sections": {}}))
