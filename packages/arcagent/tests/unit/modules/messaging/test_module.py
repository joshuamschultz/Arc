"""Unit tests for messaging module lifecycle."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from arcagent.modules.messaging import MessagingModule
from tests.unit.modules.messaging.conftest import (
    make_config_dict,
    make_ctx,
    make_team_config,
)


def _make_module(
    tmp_path: Path,
    entity_id: str = "agent://test_agent",
) -> MessagingModule:
    """Create a MessagingModule with test config."""
    config = make_config_dict(entity_id=entity_id)
    team_config = make_team_config(str(tmp_path / "team"))
    return MessagingModule(
        config=config,
        team_config=team_config,
        telemetry=MagicMock(),
        workspace=tmp_path,
    )


class TestModuleProtocol:
    def test_has_name(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        assert module.name == "messaging"

    def test_has_startup(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        assert hasattr(module, "startup")
        assert callable(module.startup)

    def test_has_shutdown(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        assert hasattr(module, "shutdown")
        assert callable(module.shutdown)


class TestModuleStartup:
    @pytest.mark.asyncio
    async def test_startup_registers_tools(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)
        await module.startup(ctx)
        try:
            # Should register 7 tools: 5 messaging + 2 team file tools.
            assert ctx.tool_registry.register.call_count == 7
        finally:
            await module.shutdown()

    @pytest.mark.asyncio
    async def test_startup_subscribes_to_events(
        self,
        tmp_path: Path,
    ) -> None:
        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)
        await module.startup(ctx)
        try:
            subscribed = [call.args[0] for call in ctx.bus.subscribe.call_args_list]
            assert "agent:assemble_prompt" in subscribed
            assert "agent:shutdown" in subscribed
        finally:
            await module.shutdown()

    @pytest.mark.asyncio
    async def test_startup_registers_entity(
        self,
        tmp_path: Path,
    ) -> None:
        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)
        await module.startup(ctx)
        try:
            # Entity should be in the registry
            entity = await module._registry.get("agent://test_agent")
            assert entity is not None
            assert entity.name == "Test Agent"
        finally:
            await module.shutdown()

    @pytest.mark.asyncio
    async def test_startup_starts_poll_task(
        self,
        tmp_path: Path,
    ) -> None:
        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)
        await module.startup(ctx)
        try:
            assert module._poll_task is not None
            assert not module._poll_task.done()
        finally:
            await module.shutdown()


class TestModuleShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_cancels_poll(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)
        await module.startup(ctx)
        await module.shutdown()
        assert module._poll_task is None

    @pytest.mark.asyncio
    async def test_double_shutdown_is_safe(
        self,
        tmp_path: Path,
    ) -> None:
        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)
        await module.startup(ctx)
        await module.shutdown()
        await module.shutdown()
        assert module._poll_task is None


class TestEntityIdFallback:
    @pytest.mark.asyncio
    async def test_entity_id_from_agent_name(
        self,
        tmp_path: Path,
    ) -> None:
        """When entity_id is empty, derive from agent config name."""
        config = make_config_dict(entity_id="")
        team_config = make_team_config(str(tmp_path / "team"))
        module = MessagingModule(
            config=config,
            team_config=team_config,
            telemetry=MagicMock(),
            workspace=tmp_path,
        )
        ctx = make_ctx(tmp_path)
        ctx.config.agent.name = "my_agent"
        await module.startup(ctx)
        try:
            assert module._config.entity_id == "agent://my_agent"
        finally:
            await module.shutdown()


class TestTeamRootResolution:
    def test_team_root_from_absolute_path(self, tmp_path: Path) -> None:
        """Absolute team_config.root is used as-is."""
        team_config = make_team_config(str(tmp_path / "custom_team"))
        workspace = tmp_path / "workspace"
        module = MessagingModule(
            config=make_config_dict(),
            team_config=team_config,
            workspace=workspace,
        )
        assert module._resolve_team_root() == tmp_path / "custom_team"

    def test_team_root_relative_resolved_against_agent_dir(self, tmp_path: Path) -> None:
        """Relative team root resolves against agent dir (workspace parent)."""
        team_config = make_team_config("shared")
        workspace = tmp_path / "workspace"
        module = MessagingModule(
            config=make_config_dict(),
            team_config=team_config,
            workspace=workspace,
        )
        assert module._resolve_team_root() == tmp_path / "shared"

    def test_team_root_parent_traversal(self, tmp_path: Path) -> None:
        """../shared resolves to sibling of agent dir, matching real layout."""
        agent_dir = tmp_path / "brad_agent"
        workspace = agent_dir / "workspace"
        team_config = make_team_config("../shared")
        module = MessagingModule(
            config=make_config_dict(),
            team_config=team_config,
            workspace=workspace,
        )
        resolved = module._resolve_team_root().resolve()
        assert resolved == (tmp_path / "shared").resolve()

    def test_team_root_fallback_when_no_team_config(self, tmp_path: Path) -> None:
        """Falls back to agent_dir/team when no team_config provided."""
        workspace = tmp_path / "workspace"
        module = MessagingModule(
            config=make_config_dict(),
            workspace=workspace,
        )
        assert module._resolve_team_root() == tmp_path / "team"

    def test_team_root_fallback_when_empty(self, tmp_path: Path) -> None:
        """Falls back to agent_dir/team when team_config.root is empty."""
        team_config = make_team_config("")
        workspace = tmp_path / "workspace"
        module = MessagingModule(
            config=make_config_dict(),
            team_config=team_config,
            workspace=workspace,
        )
        assert module._resolve_team_root() == tmp_path / "team"
