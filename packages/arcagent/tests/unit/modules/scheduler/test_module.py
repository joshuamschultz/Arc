"""Unit tests for scheduler module lifecycle — SPEC-002 Phase 5."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcagent.modules.scheduler import SchedulerModule
from tests.unit.modules.scheduler.conftest import make_config, make_ctx


def _make_module(tmp_path: Path) -> tuple[SchedulerModule, MagicMock]:
    """Create a SchedulerModule with mock dependencies."""
    config = make_config()
    telemetry = MagicMock()
    module = SchedulerModule(
        config=config,
        telemetry=telemetry,
        workspace=tmp_path,
    )
    return module, config


class TestModuleProtocol:
    def test_has_name(self, tmp_path: Path) -> None:
        module, _ = _make_module(tmp_path)
        assert module.name == "scheduler"

    def test_has_startup(self, tmp_path: Path) -> None:
        module, _ = _make_module(tmp_path)
        assert hasattr(module, "startup")
        assert callable(module.startup)

    def test_has_shutdown(self, tmp_path: Path) -> None:
        module, _ = _make_module(tmp_path)
        assert hasattr(module, "shutdown")
        assert callable(module.shutdown)


class TestModuleStartup:
    @pytest.mark.asyncio
    async def test_startup_registers_tools(self, tmp_path: Path) -> None:
        module, _ = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)
        await module.startup(ctx)
        # Should register 4 tools
        assert ctx.tool_registry.register.call_count == 4

    @pytest.mark.asyncio
    async def test_startup_subscribes_to_shutdown(self, tmp_path: Path) -> None:
        module, _ = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)
        await module.startup(ctx)
        # Should subscribe to agent:shutdown
        ctx.bus.subscribe.assert_called()
        subscribed_events = [call.args[0] for call in ctx.bus.subscribe.call_args_list]
        assert "agent:shutdown" in subscribed_events


class TestModuleShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_stops_engine(self, tmp_path: Path) -> None:
        module, _ = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)
        await module.startup(ctx)
        # Engine should be running after startup
        assert module._engine is not None
        await module.shutdown()
        # Engine should be None after shutdown (double-shutdown guard)
        assert module._engine is None

    @pytest.mark.asyncio
    async def test_double_shutdown_is_safe(self, tmp_path: Path) -> None:
        """Calling shutdown() twice should not raise."""
        module, _ = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)
        await module.startup(ctx)
        await module.shutdown()
        await module.shutdown()  # second call should be a no-op
        assert module._engine is None

    @pytest.mark.asyncio
    async def test_set_agent_run_fn_via_public_api(self, tmp_path: Path) -> None:
        """set_agent_run_fn should use engine's public setter."""
        module, _ = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)
        await module.startup(ctx)

        new_fn = AsyncMock(return_value="new")
        module.set_agent_run_fn(new_fn)
        # Verify the engine's callback was updated
        assert module._engine is not None
        assert module._engine._agent_run_fn is new_fn
