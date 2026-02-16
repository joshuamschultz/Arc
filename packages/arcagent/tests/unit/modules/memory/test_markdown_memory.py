"""Tests for MarkdownMemoryModule — main module, hook routing, event handling."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcagent.core.config import EvalConfig, MemoryConfig
from arcagent.core.module_bus import EventContext, ModuleBus
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


def _make_ctx(
    event: str, data: dict[str, Any] | None = None
) -> EventContext:
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
        await module.startup(bus)
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
        await module.startup(bus)
        assert bus.handler_count("agent:pre_tool") == 1

    @pytest.mark.asyncio()
    async def test_post_tool_handler_registered(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        bus = ModuleBus(config=MagicMock(), telemetry=MagicMock())
        await module.startup(bus)
        assert bus.handler_count("agent:post_tool") == 1

    @pytest.mark.asyncio()
    async def test_assemble_prompt_handler_registered(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        bus = ModuleBus(config=MagicMock(), telemetry=MagicMock())
        await module.startup(bus)
        assert bus.handler_count("agent:assemble_prompt") == 1

    @pytest.mark.asyncio()
    async def test_post_respond_handler_registered(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        bus = ModuleBus(config=MagicMock(), telemetry=MagicMock())
        await module.startup(bus)
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
        ctx = _make_ctx("agent:pre_tool", {
            "tool": "write",
            "args": {"path": str(tmp_path / "notes" / "test.md")},
        })
        # Should return early without processing
        await module._on_pre_tool(ctx)
        assert not ctx.is_vetoed  # No veto because guard returned early

    @pytest.mark.asyncio()
    async def test_hook_flag_resets_after_processing(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        ctx = _make_ctx("agent:pre_tool", {
            "tool": "read",
            "args": {"path": str(tmp_path / "identity.md")},
        })
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
        ctx = _make_ctx("agent:pre_tool", {
            "tool": "write",
            "args": {"path": str(tmp_path / "notes" / "2026-02-15.md")},
        })
        await module._on_pre_tool(ctx)
        # Write to notes should be vetoed (append-only enforcement)
        assert ctx.is_vetoed

    @pytest.mark.asyncio()
    async def test_identity_path_routes_to_auditor(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        identity = tmp_path / "identity.md"
        identity.write_text("old content")
        ctx = _make_ctx("agent:pre_tool", {
            "tool": "write",
            "args": {"path": str(identity)},
        })
        await module._on_pre_tool(ctx)
        # Identity writes are allowed (not vetoed), just audited
        assert not ctx.is_vetoed

    @pytest.mark.asyncio()
    async def test_non_workspace_path_ignored(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        ctx = _make_ctx("agent:pre_tool", {
            "tool": "write",
            "args": {"path": "/tmp/random_file.txt"},
        })
        await module._on_pre_tool(ctx)
        assert not ctx.is_vetoed

    @pytest.mark.asyncio()
    async def test_context_md_routes_to_guard(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        ctx = _make_ctx("agent:pre_tool", {
            "tool": "write",
            "args": {
                "path": str(tmp_path / "context.md"),
                "content": "short content",
            },
        })
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
