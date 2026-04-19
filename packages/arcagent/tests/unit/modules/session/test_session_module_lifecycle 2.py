"""Unit tests for SessionModule lifecycle — startup, shutdown, tool registration.

Coverage targets:
- startup() constructs SessionIndex with correct paths from workspace
- startup() registers build_session_search_tool into ctx.tool_registry
- shutdown() calls index.stop and set_index(None)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcagent.core.config import AgentConfig, ArcAgentConfig, LLMConfig
from arcagent.core.module_bus import ModuleBus, ModuleContext
from arcagent.modules.session import SessionModule

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture
def tool_registry() -> MagicMock:
    return MagicMock()


@pytest.fixture
def telemetry() -> MagicMock:
    return MagicMock()


@pytest.fixture
def bus() -> ModuleBus:
    return ModuleBus()


@pytest.fixture
def module_ctx(
    bus: ModuleBus,
    tool_registry: MagicMock,
    workspace: Path,
    telemetry: MagicMock,
) -> ModuleContext:
    return ModuleContext(
        bus=bus,
        tool_registry=tool_registry,
        config=ArcAgentConfig(
            agent=AgentConfig(name="test-agent"),
            llm=LLMConfig(model="test/model"),
        ),
        telemetry=telemetry,
        workspace=workspace,
        llm_config=LLMConfig(model="test/model"),
    )


# ---------------------------------------------------------------------------
# Module protocol
# ---------------------------------------------------------------------------


class TestModuleProtocol:
    def test_name(self, workspace: Path) -> None:
        m = SessionModule(workspace=workspace)
        assert m.name == "session"

    def test_default_poll_interval(self, workspace: Path) -> None:
        m = SessionModule(workspace=workspace)
        assert m._poll_interval == 30.0

    def test_custom_poll_interval(self, workspace: Path) -> None:
        m = SessionModule(config={"poll_interval": 5.0}, workspace=workspace)
        assert m._poll_interval == 5.0


# ---------------------------------------------------------------------------
# startup() — constructs SessionIndex with correct paths
# ---------------------------------------------------------------------------


class TestStartup:
    @pytest.mark.asyncio
    async def test_startup_constructs_session_index_with_correct_paths(
        self,
        workspace: Path,
        module_ctx: ModuleContext,
    ) -> None:
        """SessionIndex must be constructed with sessions_dir and db_path under workspace."""
        m = SessionModule(workspace=workspace)

        mock_index = AsyncMock()
        mock_index.start = AsyncMock()

        with patch(
            "arcagent.modules.session.SessionIndex", return_value=mock_index
        ) as mock_cls:
            with patch("arcagent.modules.session.set_index"):
                with patch("arcagent.modules.session.build_session_search_tool", return_value=MagicMock()):
                    await m.startup(module_ctx)

        mock_cls.assert_called_once()
        call_kwargs = mock_cls.call_args.kwargs
        expected_sessions_dir = workspace / "sessions"
        expected_db_path = expected_sessions_dir / "index.db"
        assert call_kwargs["sessions_dir"] == expected_sessions_dir
        assert call_kwargs["db_path"] == expected_db_path

    @pytest.mark.asyncio
    async def test_startup_calls_index_start(
        self,
        workspace: Path,
        module_ctx: ModuleContext,
    ) -> None:
        m = SessionModule(workspace=workspace)
        mock_index = AsyncMock()
        mock_index.start = AsyncMock()

        with patch("arcagent.modules.session.SessionIndex", return_value=mock_index):
            with patch("arcagent.modules.session.set_index"):
                with patch("arcagent.modules.session.build_session_search_tool", return_value=MagicMock()):
                    await m.startup(module_ctx)

        mock_index.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_startup_registers_tool_into_registry(
        self,
        workspace: Path,
        module_ctx: ModuleContext,
        tool_registry: MagicMock,
    ) -> None:
        """build_session_search_tool result must be registered into ctx.tool_registry."""
        m = SessionModule(workspace=workspace)
        mock_index = AsyncMock()
        mock_index.start = AsyncMock()
        fake_tool = MagicMock(name="session_search_tool")

        with patch("arcagent.modules.session.SessionIndex", return_value=mock_index):
            with patch("arcagent.modules.session.set_index"):
                with patch(
                    "arcagent.modules.session.build_session_search_tool",
                    return_value=fake_tool,
                ):
                    await m.startup(module_ctx)

        tool_registry.register.assert_called_once_with(fake_tool)

    @pytest.mark.asyncio
    async def test_startup_calls_set_index_with_index(
        self,
        workspace: Path,
        module_ctx: ModuleContext,
    ) -> None:
        m = SessionModule(workspace=workspace)
        mock_index = AsyncMock()
        mock_index.start = AsyncMock()

        with patch("arcagent.modules.session.SessionIndex", return_value=mock_index):
            with patch("arcagent.modules.session.set_index") as mock_set:
                with patch(
                    "arcagent.modules.session.build_session_search_tool",
                    return_value=MagicMock(),
                ):
                    await m.startup(module_ctx)

        mock_set.assert_called_once_with(mock_index)


# ---------------------------------------------------------------------------
# shutdown() — stops index and clears set_index
# ---------------------------------------------------------------------------


class TestShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_calls_index_stop(
        self,
        workspace: Path,
        module_ctx: ModuleContext,
    ) -> None:
        m = SessionModule(workspace=workspace)
        mock_index = AsyncMock()
        mock_index.start = AsyncMock()
        mock_index.stop = AsyncMock()

        with patch("arcagent.modules.session.SessionIndex", return_value=mock_index):
            with patch("arcagent.modules.session.set_index"):
                with patch(
                    "arcagent.modules.session.build_session_search_tool",
                    return_value=MagicMock(),
                ):
                    await m.startup(module_ctx)

        with patch("arcagent.modules.session.set_index") as mock_set:
            await m.shutdown()

        mock_index.stop.assert_called_once()
        # set_index(None) must be called to clear the global reference
        mock_set.assert_called_once_with(None)

    @pytest.mark.asyncio
    async def test_shutdown_clears_internal_index(
        self,
        workspace: Path,
        module_ctx: ModuleContext,
    ) -> None:
        m = SessionModule(workspace=workspace)
        mock_index = AsyncMock()
        mock_index.start = AsyncMock()
        mock_index.stop = AsyncMock()

        with patch("arcagent.modules.session.SessionIndex", return_value=mock_index):
            with patch("arcagent.modules.session.set_index"):
                with patch(
                    "arcagent.modules.session.build_session_search_tool",
                    return_value=MagicMock(),
                ):
                    await m.startup(module_ctx)

        assert m._index is mock_index
        with patch("arcagent.modules.session.set_index"):
            await m.shutdown()
        assert m._index is None

    @pytest.mark.asyncio
    async def test_shutdown_without_startup_is_safe(
        self,
        workspace: Path,
    ) -> None:
        """shutdown() before startup() must not raise (index is None)."""
        m = SessionModule(workspace=workspace)
        with patch("arcagent.modules.session.set_index"):
            await m.shutdown()  # must not raise
