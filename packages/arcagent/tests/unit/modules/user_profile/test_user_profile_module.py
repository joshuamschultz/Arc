"""Unit tests for UserProfileModule — bus wiring, read/write API, GDPR tombstone.

Coverage targets (SDD §3.6):
- startup() registers subscribers at priority 120
- read_user_profile emits an audit event
- write_user_profile delegates to store.append_durable_fact for durable_facts,
  raises ValueError for 'derived', creates default for new identity/preferences
- tombstone_user delegates to apply_tombstone
- _on_user_forgotten with missing user_did is a warning no-op
- _audit swallows telemetry exceptions
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from arcagent.core.config import AgentConfig, ArcAgentConfig, LLMConfig
from arcagent.core.module_bus import EventContext, ModuleBus, ModuleContext
from arcagent.modules.user_profile.user_profile_module import UserProfileModule

_TEST_USER_DID = "did:arc:user:human/tester-001"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture
def telemetry() -> MagicMock:
    t = MagicMock()
    t.emit_event = MagicMock()
    return t


@pytest.fixture
def bus() -> ModuleBus:
    return ModuleBus()


@pytest.fixture
def tool_registry() -> MagicMock:
    return MagicMock()


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


@pytest.fixture
def module() -> UserProfileModule:
    # UserProfileModule only takes an optional config dict.
    # Telemetry comes through ctx at startup().
    return UserProfileModule()


def _mock_profile(user_did: str = _TEST_USER_DID) -> MagicMock:
    """Return a MagicMock that stands in for a UserProfile instance."""
    p = MagicMock()
    p.user_did = user_did
    return p


# ---------------------------------------------------------------------------
# Module name / protocol
# ---------------------------------------------------------------------------


class TestModuleProtocol:
    def test_name(self, module: UserProfileModule) -> None:
        assert module.name == "user_profile"

    @pytest.mark.asyncio
    async def test_shutdown_is_noop(self, module: UserProfileModule) -> None:
        await module.shutdown()  # must not raise


# ---------------------------------------------------------------------------
# startup() — bus subscriptions at priority 120
# ---------------------------------------------------------------------------


class TestStartup:
    @pytest.mark.asyncio
    async def test_registers_post_respond_subscriber(
        self,
        module: UserProfileModule,
        module_ctx: ModuleContext,
        bus: ModuleBus,
    ) -> None:
        await module.startup(module_ctx)
        assert bus.handler_count("agent:post_respond") >= 1

    @pytest.mark.asyncio
    async def test_registers_user_forgotten_subscriber(
        self,
        module: UserProfileModule,
        module_ctx: ModuleContext,
        bus: ModuleBus,
    ) -> None:
        await module.startup(module_ctx)
        assert bus.handler_count("user.forgotten") >= 1

    @pytest.mark.asyncio
    async def test_startup_creates_store(
        self,
        module: UserProfileModule,
        module_ctx: ModuleContext,
    ) -> None:
        assert module._store is None
        await module.startup(module_ctx)
        assert module._store is not None

    @pytest.mark.asyncio
    async def test_startup_wires_workspace(
        self,
        module: UserProfileModule,
        module_ctx: ModuleContext,
        workspace: Path,
    ) -> None:
        await module.startup(module_ctx)
        assert module._workspace == workspace

    @pytest.mark.asyncio
    async def test_startup_wires_telemetry(
        self,
        module: UserProfileModule,
        module_ctx: ModuleContext,
        telemetry: MagicMock,
    ) -> None:
        await module.startup(module_ctx)
        assert module._telemetry is telemetry


# ---------------------------------------------------------------------------
# read_user_profile — emits audit event
# ---------------------------------------------------------------------------


class TestReadUserProfile:
    @pytest.mark.asyncio
    async def test_read_emits_audit_event(
        self,
        module: UserProfileModule,
        module_ctx: ModuleContext,
        telemetry: MagicMock,
    ) -> None:
        await module.startup(module_ctx)
        mock_profile = _mock_profile()
        with patch.object(module._store, "read", return_value=mock_profile):  # type: ignore[union-attr]
            result = module.read_user_profile(_TEST_USER_DID)

        telemetry.emit_event.assert_called_once_with(
            "memory.user_profile.read",
            {"user_did": _TEST_USER_DID},
        )
        assert result is mock_profile

    def test_read_before_startup_raises_runtime_error(self, module: UserProfileModule) -> None:
        with pytest.raises(RuntimeError, match="startup"):
            module.read_user_profile(_TEST_USER_DID)


# ---------------------------------------------------------------------------
# write_user_profile
# ---------------------------------------------------------------------------


class TestWriteUserProfile:
    @pytest.mark.asyncio
    async def test_durable_facts_delegates_to_append(
        self,
        module: UserProfileModule,
        module_ctx: ModuleContext,
        telemetry: MagicMock,
    ) -> None:
        await module.startup(module_ctx)
        mock_profile = _mock_profile()
        with patch.object(
            module._store,
            "append_durable_fact",
            return_value=mock_profile,  # type: ignore[union-attr]
        ) as mock_append:
            result = module.write_user_profile(_TEST_USER_DID, "durable_facts", "new fact")
        mock_append.assert_called_once()
        assert result is mock_profile

    @pytest.mark.asyncio
    async def test_derived_section_raises_value_error(
        self,
        module: UserProfileModule,
        module_ctx: ModuleContext,
    ) -> None:
        await module.startup(module_ctx)
        with pytest.raises(ValueError, match="Derived"):
            module.write_user_profile(_TEST_USER_DID, "derived", "content")

    @pytest.mark.asyncio
    async def test_unknown_section_raises_value_error(
        self,
        module: UserProfileModule,
        module_ctx: ModuleContext,
    ) -> None:
        await module.startup(module_ctx)
        with pytest.raises(ValueError, match="Unknown section"):
            module.write_user_profile(_TEST_USER_DID, "bogus_section", "x")

    @pytest.mark.asyncio
    async def test_identity_creates_default_for_new_user(
        self,
        module: UserProfileModule,
        module_ctx: ModuleContext,
    ) -> None:
        """New user_did with identity section gets a default profile created."""
        await module.startup(module_ctx)
        user_did = "did:arc:user:human/brand-new"
        mock_profile = _mock_profile(user_did)

        with patch.object(module._store, "exists", return_value=False):  # type: ignore[union-attr]
            with patch.object(
                module._store,
                "create_default",
                return_value=mock_profile,  # type: ignore[union-attr]
            ) as mock_create:
                with patch.object(module._store, "write"):  # type: ignore[union-attr]
                    module.write_user_profile(user_did, "identity", "My name is Arc")
        mock_create.assert_called_once_with(user_did)

    @pytest.mark.asyncio
    async def test_write_emits_audit_event(
        self,
        module: UserProfileModule,
        module_ctx: ModuleContext,
        telemetry: MagicMock,
    ) -> None:
        await module.startup(module_ctx)
        mock_profile = _mock_profile()
        with patch.object(module._store, "append_durable_fact", return_value=mock_profile):  # type: ignore[union-attr]
            module.write_user_profile(_TEST_USER_DID, "durable_facts", "fact")
        telemetry.emit_event.assert_called_once_with(
            "memory.user_profile.write",
            {"user_did": _TEST_USER_DID, "section": "durable_facts"},
        )

    def test_write_before_startup_raises_runtime_error(self, module: UserProfileModule) -> None:
        with pytest.raises(RuntimeError, match="startup"):
            module.write_user_profile(_TEST_USER_DID, "identity", "x")


# ---------------------------------------------------------------------------
# tombstone_user
# ---------------------------------------------------------------------------


class TestTombstoneUser:
    @pytest.mark.asyncio
    async def test_tombstone_delegates_to_apply_tombstone(
        self,
        module: UserProfileModule,
        module_ctx: ModuleContext,
    ) -> None:
        await module.startup(module_ctx)
        with patch(
            "arcagent.modules.user_profile.user_profile_module.apply_tombstone"
        ) as mock_tombstone:
            module.tombstone_user(_TEST_USER_DID)
        mock_tombstone.assert_called_once()
        assert mock_tombstone.call_args.args[0] == _TEST_USER_DID

    def test_tombstone_before_startup_raises_runtime_error(
        self, module: UserProfileModule
    ) -> None:
        with pytest.raises(RuntimeError, match="not been started"):
            module.tombstone_user(_TEST_USER_DID)


# ---------------------------------------------------------------------------
# _on_user_forgotten
# ---------------------------------------------------------------------------


class TestOnUserForgotten:
    @pytest.mark.asyncio
    async def test_missing_user_did_is_warning_noop(
        self,
        module: UserProfileModule,
        module_ctx: ModuleContext,
    ) -> None:
        """user.forgotten with empty user_did must warn and not call tombstone."""
        await module.startup(module_ctx)
        ctx = EventContext(
            event="user.forgotten",
            data={},  # missing user_did intentionally
            agent_did="did:arc:agent:test",
            trace_id="trace-1",
        )
        with patch.object(module, "tombstone_user") as mock_ts:
            await module._on_user_forgotten(ctx)
        mock_ts.assert_not_called()

    @pytest.mark.asyncio
    async def test_with_user_did_calls_tombstone(
        self,
        module: UserProfileModule,
        module_ctx: ModuleContext,
    ) -> None:
        await module.startup(module_ctx)
        ctx = EventContext(
            event="user.forgotten",
            data={"user_did": _TEST_USER_DID},
            agent_did="did:arc:agent:test",
            trace_id="trace-2",
        )
        with patch.object(module, "tombstone_user") as mock_ts:
            await module._on_user_forgotten(ctx)
        mock_ts.assert_called_once_with(_TEST_USER_DID)


# ---------------------------------------------------------------------------
# _audit — swallows telemetry exceptions
# ---------------------------------------------------------------------------


class TestAudit:
    @pytest.mark.asyncio
    async def test_audit_swallows_telemetry_exception(
        self,
        module: UserProfileModule,
        module_ctx: ModuleContext,
        telemetry: MagicMock,
    ) -> None:
        await module.startup(module_ctx)
        telemetry.emit_event.side_effect = RuntimeError("telemetry gone")
        # Must not raise even when telemetry throws
        module._audit("test.event", {"key": "value"})

    def test_audit_noop_without_telemetry(self) -> None:
        m = UserProfileModule()
        m._telemetry = None
        # Must not raise when telemetry is absent
        m._audit("test.event", {})
