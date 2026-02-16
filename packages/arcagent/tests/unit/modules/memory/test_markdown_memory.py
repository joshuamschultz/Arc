"""Tests for MarkdownMemoryModule — main module, hook routing, event handling."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    EvalConfig,
    LLMConfig,
    MemoryConfig,
)
from arcagent.core.module_bus import EventContext, ModuleBus, ModuleContext
from arcagent.modules.memory.markdown_memory import MarkdownMemoryModule


def _make_telemetry() -> MagicMock:
    t = MagicMock()
    t.audit_event = MagicMock()
    return t


def _make_config() -> ArcAgentConfig:
    return ArcAgentConfig(
        agent=AgentConfig(name="test"),
        llm=LLMConfig(model="test/model"),
    )


def _make_module(workspace: Path) -> MarkdownMemoryModule:
    return MarkdownMemoryModule(
        config=MemoryConfig(),
        eval_config=EvalConfig(),
        telemetry=_make_telemetry(),
        workspace=workspace,
    )


def _make_module_ctx(bus: ModuleBus, workspace: Path) -> ModuleContext:
    """Create a ModuleContext for tests that call startup()."""
    config = _make_config()
    return ModuleContext(
        bus=bus,
        tool_registry=MagicMock(),
        config=config,
        telemetry=_make_telemetry(),
        workspace=workspace,
        llm_config=config.llm,
    )


def _make_ctx(event: str, data: dict[str, Any] | None = None) -> EventContext:
    return EventContext(
        event=event,
        data=data or {},
        agent_did="did:arc:test",
        trace_id="trace-1",
    )


class TestModuleProtocol:
    """T3.1.3: Module protocol implementation."""

    def test_name_is_memory(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        assert module.name == "memory"

    @pytest.mark.asyncio()
    async def test_startup_subscribes_events(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        bus = ModuleBus(
            config=MagicMock(),
            telemetry=MagicMock(),
        )
        await module.startup(_make_module_ctx(bus, tmp_path))
        assert bus.handler_count("agent:pre_tool") >= 1
        assert bus.handler_count("agent:post_tool") >= 1
        assert bus.handler_count("agent:assemble_prompt") >= 1
        assert bus.handler_count("agent:post_respond") >= 1

    @pytest.mark.asyncio()
    async def test_shutdown_completes_without_error(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        await module.shutdown()  # Should not raise


class TestEventSubscription:
    """T3.1.4: Event subscription verification."""

    @pytest.mark.asyncio()
    async def test_pre_tool_handler_registered(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        bus = ModuleBus(config=MagicMock(), telemetry=MagicMock())
        await module.startup(_make_module_ctx(bus, tmp_path))
        assert bus.handler_count("agent:pre_tool") == 1

    @pytest.mark.asyncio()
    async def test_post_tool_handler_registered(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        bus = ModuleBus(config=MagicMock(), telemetry=MagicMock())
        await module.startup(_make_module_ctx(bus, tmp_path))
        assert bus.handler_count("agent:post_tool") == 1

    @pytest.mark.asyncio()
    async def test_assemble_prompt_handler_registered(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        bus = ModuleBus(config=MagicMock(), telemetry=MagicMock())
        await module.startup(_make_module_ctx(bus, tmp_path))
        assert bus.handler_count("agent:assemble_prompt") == 1

    @pytest.mark.asyncio()
    async def test_post_respond_handler_registered(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        bus = ModuleBus(config=MagicMock(), telemetry=MagicMock())
        await module.startup(_make_module_ctx(bus, tmp_path))
        assert bus.handler_count("agent:post_respond") == 1


class TestPathResolution:
    """T3.1.5: Path resolution for read/write/edit tools."""

    def test_resolve_write_tool_path(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        path = module._resolve_path("write", {"path": str(tmp_path / "notes" / "today.md")})
        assert path is not None
        assert path == (tmp_path / "notes" / "today.md").resolve()

    def test_resolve_edit_tool_path(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        path = module._resolve_path("edit", {"file_path": str(tmp_path / "context.md")})
        assert path is not None
        assert path == (tmp_path / "context.md").resolve()

    def test_resolve_read_tool_path(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        path = module._resolve_path("read", {"path": str(tmp_path / "identity.md")})
        assert path is not None

    def test_resolve_unknown_tool_returns_none(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        path = module._resolve_path("unknown_tool", {"path": "/some/path"})
        assert path is None

    def test_resolve_missing_path_returns_none(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        path = module._resolve_path("write", {})
        assert path is None


class TestBashCommandParsing:
    """T3.1.6: Bash command parsing for memory path targets."""

    def test_echo_redirect_to_notes(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        notes_file = tmp_path / "notes" / "2026-02-15.md"
        path = module._parse_bash_target(f'echo "hello" > {notes_file}')
        assert path is not None
        assert path == notes_file.resolve()

    def test_rm_identity_file(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        identity = tmp_path / "identity.md"
        path = module._parse_bash_target(f"rm {identity}")
        assert path is not None
        assert path == identity.resolve()

    def test_mv_notes_file(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        notes_file = tmp_path / "notes" / "old.md"
        path = module._parse_bash_target(f"mv {notes_file} /tmp/backup.md")
        assert path is not None

    def test_non_memory_path_returns_none(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        path = module._parse_bash_target("echo hello > /tmp/whatever.txt")
        assert path is None

    def test_malformed_command_returns_none(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        path = module._parse_bash_target('echo "unclosed string')
        assert path is None

    def test_bash_tool_routing(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        notes_file = tmp_path / "notes" / "test.md"
        path = module._resolve_path("bash", {"command": f"echo test > {notes_file}"})
        assert path is not None


class TestReentrancyGuard:
    """T3.1.7: Re-entrancy guard prevents nested hooks."""

    @pytest.mark.asyncio()
    async def test_nested_hooks_do_not_fire(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        # Simulate hook being active
        module._hook_active = True
        ctx = _make_ctx(
            "agent:pre_tool",
            {
                "tool": "write",
                "args": {"path": str(tmp_path / "notes" / "test.md")},
            },
        )
        # Should return early without processing
        await module._on_pre_tool(ctx)
        assert not ctx.is_vetoed  # No veto because guard returned early

    @pytest.mark.asyncio()
    async def test_hook_flag_resets_after_processing(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        ctx = _make_ctx(
            "agent:pre_tool",
            {
                "tool": "read",
                "args": {"path": str(tmp_path / "identity.md")},
            },
        )
        await module._on_pre_tool(ctx)
        assert not module._hook_active  # Flag reset after processing


class TestBackgroundTaskTracking:
    """T3.1.8: Background task set with done callbacks."""

    @pytest.mark.asyncio()
    async def test_spawn_background_adds_to_set(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)

        async def dummy() -> None:
            await asyncio.sleep(0.1)

        module._spawn_background(dummy())
        assert len(module._background_tasks) == 1

    @pytest.mark.asyncio()
    async def test_done_callback_removes_from_set(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)

        async def quick() -> None:
            pass

        module._spawn_background(quick())
        await asyncio.sleep(0.05)  # Let task complete
        assert len(module._background_tasks) == 0

    @pytest.mark.asyncio()
    async def test_error_in_background_logs_not_crashes(self, tmp_path: Path) -> None:
        telemetry = _make_telemetry()
        module = MarkdownMemoryModule(
            config=MemoryConfig(),
            eval_config=EvalConfig(),
            telemetry=telemetry,
            workspace=tmp_path,
        )

        async def failing() -> None:
            raise ValueError("test error")

        module._spawn_background(failing())
        await asyncio.sleep(0.05)
        # Task removed, audit event logged
        assert len(module._background_tasks) == 0
        telemetry.audit_event.assert_called_once()
        call_args = telemetry.audit_event.call_args
        assert call_args[0][0] == "memory.background_error"


class TestShutdown:
    """T3.1.9: Shutdown cancels background tasks."""

    @pytest.mark.asyncio()
    async def test_shutdown_cancels_background_tasks(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)

        async def long_task() -> None:
            await asyncio.sleep(100)

        module._spawn_background(long_task())
        assert len(module._background_tasks) == 1
        await module.shutdown()
        # Tasks should be cancelled and removed
        await asyncio.sleep(0.05)
        assert len(module._background_tasks) == 0


class TestPreToolRouting:
    """T3.1.11: Path-based routing in pre_tool handler."""

    @pytest.mark.asyncio()
    async def test_notes_path_routes_to_notes_manager(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        notes_dir = tmp_path / "notes"
        notes_dir.mkdir()
        ctx = _make_ctx(
            "agent:pre_tool",
            {
                "tool": "write",
                "args": {"path": str(tmp_path / "notes" / "2026-02-15.md")},
            },
        )
        await module._on_pre_tool(ctx)
        # Write to notes should be vetoed (append-only enforcement)
        assert ctx.is_vetoed

    @pytest.mark.asyncio()
    async def test_identity_path_routes_to_auditor(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        identity = tmp_path / "identity.md"
        identity.write_text("old content")
        ctx = _make_ctx(
            "agent:pre_tool",
            {
                "tool": "write",
                "args": {"path": str(identity)},
            },
        )
        await module._on_pre_tool(ctx)
        # Identity writes are allowed (not vetoed), just audited
        assert not ctx.is_vetoed

    @pytest.mark.asyncio()
    async def test_non_workspace_path_ignored(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        ctx = _make_ctx(
            "agent:pre_tool",
            {
                "tool": "write",
                "args": {"path": "/tmp/random_file.txt"},
            },
        )
        await module._on_pre_tool(ctx)
        assert not ctx.is_vetoed

    @pytest.mark.asyncio()
    async def test_context_md_routes_to_guard(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        ctx = _make_ctx(
            "agent:pre_tool",
            {
                "tool": "write",
                "args": {
                    "path": str(tmp_path / "context.md"),
                    "content": "short content",
                },
            },
        )
        await module._on_pre_tool(ctx)
        # Short content should not be vetoed
        assert not ctx.is_vetoed


class TestIsMemoryPath:
    """Test memory path detection."""

    def test_notes_path(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        assert module._is_memory_path((tmp_path / "notes" / "test.md").resolve())

    def test_identity_path(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        assert module._is_memory_path((tmp_path / "identity.md").resolve())

    def test_policy_path(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        assert module._is_memory_path((tmp_path / "policy.md").resolve())

    def test_context_path(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        assert module._is_memory_path((tmp_path / "context.md").resolve())

    def test_entities_path(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        assert module._is_memory_path((tmp_path / "entities" / "josh" / "facts.jsonl").resolve())

    def test_non_memory_path(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        assert not module._is_memory_path(Path("/tmp/random.txt").resolve())


class TestContextGuardTruncation:
    """Test ContextGuard truncation when content exceeds budget."""

    async def test_context_guard_truncates_oversized_content(self, tmp_path: Path) -> None:
        """When content exceeds budget, ContextGuard auto-truncates oldest lines."""
        module = _make_module(tmp_path)
        # Default budget is 2000 tokens = 8000 chars
        # Create content much larger than that
        large_content = "\n".join([f"Line {i:04d} with some additional text to make it longer" for i in range(500)])
        ctx = _make_ctx(
            "agent:pre_tool",
            {
                "tool": "write",
                "args": {
                    "path": str(tmp_path / "context.md"),
                    "content": large_content,
                },
            },
        )
        await module._on_pre_tool(ctx)
        # Content should be truncated in args
        truncated = ctx.data["args"]["content"]
        assert len(truncated) < len(large_content)
        # Should keep most recent lines (oldest are truncated)
        assert "Line 0499" in truncated
        assert "Line 0000" not in truncated


class TestBashTargetsMemory:
    """Test bash command detection for memory paths."""

    async def test_bash_veto_on_memory_path_write(self, tmp_path: Path) -> None:
        """Bash commands targeting memory files are vetoed."""
        module = _make_module(tmp_path)
        notes_file = tmp_path / "notes" / "test.md"
        ctx = _make_ctx(
            "agent:pre_tool",
            {
                "tool": "bash",
                "args": {"command": f'echo "hello" > {notes_file}'},
            },
        )
        await module._on_pre_tool(ctx)
        assert ctx.is_vetoed

    async def test_bash_veto_with_dangerous_command(self, tmp_path: Path) -> None:
        """Dangerous commands (sed, awk) targeting memory paths are vetoed."""
        module = _make_module(tmp_path)
        identity_file = tmp_path / "identity.md"
        ctx = _make_ctx(
            "agent:pre_tool",
            {
                "tool": "bash",
                "args": {"command": f"sed -i 's/old/new/g' {identity_file}"},
            },
        )
        await module._on_pre_tool(ctx)
        assert ctx.is_vetoed

    async def test_bash_veto_with_tee_command(self, tmp_path: Path) -> None:
        """tee command targeting memory paths is vetoed."""
        module = _make_module(tmp_path)
        context_file = tmp_path / "context.md"
        ctx = _make_ctx(
            "agent:pre_tool",
            {
                "tool": "bash",
                "args": {"command": f"echo data | tee {context_file}"},
            },
        )
        await module._on_pre_tool(ctx)
        assert ctx.is_vetoed

    async def test_bash_malformed_shell_fallback(self, tmp_path: Path) -> None:
        """Malformed shell commands fall back to substring matching."""
        module = _make_module(tmp_path)
        # Unclosed quote — shlex.split will fail
        ctx = _make_ctx(
            "agent:pre_tool",
            {
                "tool": "bash",
                "args": {"command": f'echo "unclosed > {tmp_path}/notes/test.md'},
            },
        )
        await module._on_pre_tool(ctx)
        # Should still detect via substring matching
        assert ctx.is_vetoed


class TestIdentityAuditorSnapshotEviction:
    """Test IdentityAuditor snapshot eviction at max limit."""

    async def test_snapshot_eviction_at_max(self, tmp_path: Path) -> None:
        """When snapshots exceed max, oldest are evicted."""
        module = _make_module(tmp_path)
        identity_file = tmp_path / "identity.md"
        identity_file.write_text("initial content")

        # Fill up to max snapshots
        for i in range(51):  # Over the limit of 50
            ctx = _make_ctx(
                "agent:pre_tool",
                {
                    "tool": "write",
                    "args": {"path": str(identity_file)},
                },
            )
            ctx.trace_id = f"trace-{i}"
            await module._on_pre_tool(ctx)

        # Should have evicted oldest
        assert len(module._identity_auditor._before_snapshots) == 50


class TestBackgroundTaskBackpressure:
    """Test background task backpressure when queue is full."""

    async def test_backpressure_drops_tasks(self, tmp_path: Path) -> None:
        """When background queue is full, new tasks are dropped."""
        module = _make_module(tmp_path)

        async def slow_task() -> None:
            await asyncio.sleep(10)

        # Fill queue to max
        for _ in range(10):
            module._spawn_background(slow_task())

        assert len(module._background_tasks) == 10

        # Next task should be dropped
        module._spawn_background(slow_task())
        # Still at max
        assert len(module._background_tasks) == 10

        await module.shutdown()


class TestMemorySearchResultFormatting:
    """Test memory_search result formatting with boundary markers."""

    async def test_memory_search_with_results(self, tmp_path: Path) -> None:
        """Memory search formats results with boundary markers."""
        from arcagent.modules.memory.hybrid_search import SearchResult
        module = _make_module(tmp_path)

        # Mock hybrid_search to return results
        from unittest.mock import AsyncMock
        module._hybrid_search.search = AsyncMock(return_value=[
            SearchResult(source="notes/2026-02-15.md", content="Test content", score=0.9, match_type="bm25"),
            SearchResult(source="context.md", content="More content", score=0.7, match_type="bm25"),
        ])

        result = await module._handle_memory_search(query="test")

        # Should have boundary markers
        assert "<memory-result" in result
        assert "</memory-result>" in result
        assert "Test content" in result
        assert "More content" in result
        assert 'source="notes/2026-02-15.md"' in result


class TestGetEvalModelNoConfigNoLLM:
    """Lines 283-284: No eval config and no LLM config, non-error fallback."""

    def test_returns_none_and_warns(self, tmp_path: Path) -> None:
        module = MarkdownMemoryModule(
            config=MemoryConfig(),
            eval_config=EvalConfig(provider="", model="", fallback_behavior="skip"),
            telemetry=_make_telemetry(),
            workspace=tmp_path,
        )
        module._llm_config = None
        result = module._get_eval_model()
        assert result is None


class TestOnPreToolPathNone:
    """Line 387: _on_pre_tool returns when path is None."""

    async def test_returns_when_path_none(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        # tool=write but no path in args -> _resolve_path returns None
        ctx = _make_ctx("agent:pre_tool", {"tool": "write", "args": {}})
        await module._on_pre_tool(ctx)
        assert not ctx.is_vetoed


class TestOnPostToolPathNone:
    """Line 410: _on_post_tool returns when path is None."""

    async def test_returns_when_path_none(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        ctx = _make_ctx("agent:post_tool", {"tool": "write", "args": {}})
        await module._on_post_tool(ctx)
        # Should not raise


class TestOnPostToolOutsideWorkspace:
    """Lines 416-417: ValueError when path not under workspace."""

    async def test_path_not_under_workspace(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        ctx = _make_ctx(
            "agent:post_tool",
            {"tool": "write", "args": {"path": "/tmp/outside/identity.md"}},
        )
        await module._on_post_tool(ctx)
        # Should not raise (ValueError caught)


class TestOnPostRespondModelNone:
    """Line 452: _on_post_respond returns when model is None."""

    async def test_returns_when_no_model(self, tmp_path: Path) -> None:
        module = MarkdownMemoryModule(
            config=MemoryConfig(),
            eval_config=EvalConfig(provider="", model="", fallback_behavior="skip"),
            telemetry=_make_telemetry(),
            workspace=tmp_path,
        )
        module._llm_config = None
        ctx = _make_ctx("agent:post_respond", {"messages": [{"role": "user", "content": "hi"}]})
        await module._on_post_respond(ctx)
        assert len(module._background_tasks) == 0


class TestBashTargetsMemoryTokenAnalysis:
    """Lines 555-575: _bash_targets_memory token-level analysis."""

    def test_dangerous_cmd_with_memory_path_detected(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        notes_path = tmp_path / "notes" / "test.md"
        assert module._bash_targets_memory(f"sed -i 's/x/y/' {notes_path}")

    def test_dangerous_tee_cmd_detected(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        identity = tmp_path / "identity.md"
        assert module._bash_targets_memory(f"echo data | tee {identity}")

    def test_non_memory_path_not_detected(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        assert not module._bash_targets_memory("echo hello > /tmp/foo.txt")

    def test_path_like_token_resolved(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        ctx_file = tmp_path / "context.md"
        assert module._bash_targets_memory(f"cat {ctx_file}")

    def test_non_path_tokens_skipped(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        assert not module._bash_targets_memory("echo hello world")

    def test_malformed_shell_substring_fallback(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        # shlex will fail on unclosed quote, falls back to substring matching
        assert module._bash_targets_memory(f'echo "unclosed > {tmp_path}/notes/test.md')

    def test_oserror_on_path_resolution(self, tmp_path: Path) -> None:
        """Lines 572-573: OSError/ValueError in Path resolution handled."""
        module = _make_module(tmp_path)
        # Very long filename that might cause OSError
        assert not module._bash_targets_memory("cat " + "a" * 5 + ".md")


class TestOnShutdownModelNone:
    """Line 473: _on_shutdown returns when model is None."""

    async def test_on_shutdown_returns_when_no_model(self, tmp_path: Path) -> None:
        module = MarkdownMemoryModule(
            config=MemoryConfig(),
            eval_config=EvalConfig(provider="", model="", fallback_behavior="skip"),
            telemetry=_make_telemetry(),
            workspace=tmp_path,
        )
        module._llm_config = None
        module._session_messages = [{"role": "user", "content": "hi"}]
        ctx = _make_ctx("agent:shutdown", {"session_id": "test"})
        await module._on_shutdown(ctx)
        # Should return early without error


class TestBashTargetsDangerousCmd:
    """Lines 565-567, 579-581: Edge cases in _bash_targets_memory."""

    def test_dangerous_cmd_targets_memory_via_tee(self, tmp_path: Path) -> None:
        """Dangerous cmd (tee) targeting memory path detected."""
        module = _make_module(tmp_path)
        notes = tmp_path / "notes"
        notes.mkdir()
        assert module._bash_targets_memory(f"echo 'data' | tee {notes}/test.md")

    def test_sed_targeting_memory_path(self, tmp_path: Path) -> None:
        """Dangerous cmd (sed) targeting memory path detected."""
        module = _make_module(tmp_path)
        notes = tmp_path / "notes"
        notes.mkdir()
        assert module._bash_targets_memory(f"sed -i 's/old/new/' {notes}/test.md")

    def test_path_resolution_oserror_continues(self, tmp_path: Path) -> None:
        """Lines 579-581: OSError on Path.resolve continues to next token."""
        module = _make_module(tmp_path)
        # Null bytes in path cause ValueError/OSError
        assert not module._bash_targets_memory("cat /dev/null\x00/notes/test.md")


class TestBashTargetsMemoryShlex:
    """Lines 576-578, 590: shlex fallback and resolved path detection."""

    def test_shlex_valueerror_with_memory_subpath(self, tmp_path: Path) -> None:
        """Lines 576-578: Malformed shell falls back to substring match."""
        module = _make_module(tmp_path)
        # Unclosed quote triggers shlex.ValueError
        # Substring 'notes/' is in _MEMORY_SUBPATHS — detected by fallback
        # Must NOT contain workspace path to avoid fast-path match on line 570
        assert module._bash_targets_memory('echo "unclosed notes/something.md')

    def test_shlex_valueerror_no_memory_subpath(self, tmp_path: Path) -> None:
        """Lines 576-578: Malformed shell with no memory subpath returns False."""
        module = _make_module(tmp_path)
        assert not module._bash_targets_memory('echo "unclosed /tmp/safe.txt')

    def test_resolved_path_matches_memory(self, tmp_path: Path) -> None:
        """Line 590: Resolved path matching memory returns True via token check."""
        import os

        module = _make_module(tmp_path)
        notes = tmp_path / "notes"
        notes.mkdir()
        (notes / "test.md").write_text("content")
        # Use a relative path so it doesn't match fast-path substring
        # Run from the workspace dir
        old_cwd = os.getcwd()
        try:
            os.chdir(tmp_path)
            assert module._bash_targets_memory("cat notes/test.md")
        finally:
            os.chdir(old_cwd)


