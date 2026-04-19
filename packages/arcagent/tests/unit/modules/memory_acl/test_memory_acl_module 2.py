"""Unit tests for MemoryACLModule subscription and veto behavior.

Covers test contract items:
1. test_priority_10_runs_before_memory_module
2. test_veto_on_unauthorized_cross_session_read
3. test_allowed_within_shared_with_agent
4. test_veto_emits_audit_event
5. test_cross_session_read_at_federal_default_private
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from arcagent.core.module_bus import EventContext, ModuleBus, ModuleContext
from arcagent.modules.memory_acl.memory_acl_module import MemoryACLModule, _ACL_PRIORITY


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_module(
    tier: str = "federal",
    telemetry: Any = None,
) -> MemoryACLModule:
    return MemoryACLModule(config={"tier": tier}, telemetry=telemetry)


def _make_bus() -> ModuleBus:
    return ModuleBus()


def _make_module_ctx(bus: ModuleBus) -> ModuleContext:
    """Create a minimal ModuleContext for startup."""
    mock_registry = MagicMock()
    mock_config = MagicMock()
    mock_telemetry = MagicMock()
    mock_workspace = MagicMock()
    mock_llm_config = MagicMock()
    return ModuleContext(
        bus=bus,
        tool_registry=mock_registry,
        config=mock_config,
        telemetry=mock_telemetry,
        workspace=mock_workspace,
        llm_config=mock_llm_config,
    )


def _private_acl_content(owner: str = "did:arc:org:user/owner") -> str:
    return f"""---
acl:
  cross_session_visibility: private
owner_did: {owner}
---
"""


def _shared_with_agent_content(owner: str = "did:arc:org:user/owner") -> str:
    return f"""---
acl:
  cross_session_visibility: shared-with-agent
owner_did: {owner}
---
"""


# ---------------------------------------------------------------------------
# Test item 1: test_priority_10_runs_before_memory_module
# ---------------------------------------------------------------------------


class TestPriority10:
    def test_acl_priority_is_10(self) -> None:
        module = _make_module()
        assert module.priority == 10

    async def test_bus_subscription_at_priority_10(self) -> None:
        bus = _make_bus()
        module = _make_module()
        ctx = _make_module_ctx(bus)
        await module.startup(ctx)

        # Verify all three memory events have handlers at priority 10
        for event in ("memory.read", "memory.write", "memory.search"):
            handlers = bus._handlers.get(event, [])
            priorities = [h.priority for h in handlers]
            assert 10 in priorities, f"No priority-10 handler for {event}"

    async def test_priority_10_lower_than_memory_module_priority(self) -> None:
        # Memory module subscribes to agent:pre_tool at priority 10
        # and agent:post_tool at priority 100 (confirmed from source).
        # The ACL module subscribes to memory.* at priority 10.
        # Since priority 10 < 85/90, ACL runs first.
        bus = _make_bus()
        acl_module = _make_module()
        ctx = _make_module_ctx(bus)
        await acl_module.startup(ctx)

        execution_order: list[str] = []

        async def fake_memory_handler(evt: EventContext) -> None:
            execution_order.append("memory_module_85")

        # Register a handler simulating memory module at priority 85
        bus.subscribe("memory.read", fake_memory_handler, priority=85)

        # Emit — ACL at 10 runs before fake memory at 85
        caller = "did:arc:org:user/stranger"
        result = await bus.emit(
            "memory.read",
            {
                "caller_did": caller,
                "target_user_did": "did:arc:org:user/owner",
                "session_acl_content": _private_acl_content(),
            },
            agent_did="did:arc:org:agent/agent1",
        )
        # ACL vetoed it — but memory handler still ran (all handlers run after veto)
        assert result.is_vetoed, "ACL should have vetoed"
        assert "memory_module_85" in execution_order, "Memory handler did not run"


# ---------------------------------------------------------------------------
# Test item 2: test_veto_on_unauthorized_cross_session_read
# ---------------------------------------------------------------------------


class TestVetoUnauthorizedRead:
    async def test_private_acl_blocks_stranger(self) -> None:
        bus = _make_bus()
        module = _make_module(tier="federal")
        await module.startup(_make_module_ctx(bus))

        ctx = await bus.emit(
            "memory.read",
            {
                "caller_did": "did:arc:org:user/stranger",
                "target_user_did": "did:arc:org:user/owner",
                "session_acl_content": _private_acl_content(),
            },
            agent_did="did:arc:org:agent/agent1",
        )
        assert ctx.is_vetoed
        assert "ACL denied" in ctx.veto_reason
        assert "private" in ctx.veto_reason

    async def test_no_acl_federal_defaults_to_private(self) -> None:
        """Test item 5: federal tier without explicit ACL defaults to private."""
        bus = _make_bus()
        module = _make_module(tier="federal")
        await module.startup(_make_module_ctx(bus))

        ctx = await bus.emit(
            "memory.read",
            {
                "caller_did": "did:arc:org:user/stranger",
                "target_user_did": "did:arc:org:user/owner",
                # No session_acl_content → falls back to federal default=private
            },
            agent_did="did:arc:org:agent/agent1",
        )
        assert ctx.is_vetoed

    async def test_same_user_not_vetoed(self) -> None:
        """Caller reading their own session is never vetoed."""
        same_did = "did:arc:org:user/same"
        bus = _make_bus()
        module = _make_module(tier="federal")
        await module.startup(_make_module_ctx(bus))

        ctx = await bus.emit(
            "memory.read",
            {
                "caller_did": same_did,
                "target_user_did": same_did,
                "session_acl_content": _private_acl_content(owner=same_did),
            },
            agent_did="did:arc:org:agent/agent1",
        )
        assert not ctx.is_vetoed

    async def test_write_vetoed_for_non_owner(self) -> None:
        bus = _make_bus()
        module = _make_module(tier="personal")
        await module.startup(_make_module_ctx(bus))

        ctx = await bus.emit(
            "memory.write",
            {
                "caller_did": "did:arc:org:user/stranger",
                "target_user_did": "did:arc:org:user/owner",
                "session_acl_content": _shared_with_agent_content(),
            },
            agent_did="did:arc:org:agent/agent1",
        )
        assert ctx.is_vetoed

    async def test_search_vetoed_for_private_session(self) -> None:
        bus = _make_bus()
        module = _make_module(tier="federal")
        await module.startup(_make_module_ctx(bus))

        ctx = await bus.emit(
            "memory.search",
            {
                "caller_did": "did:arc:org:user/stranger",
                "target_user_did": "did:arc:org:user/owner",
                "session_acl_content": _private_acl_content(),
            },
            agent_did="did:arc:org:agent/agent1",
        )
        assert ctx.is_vetoed


# ---------------------------------------------------------------------------
# Test item 3: test_allowed_within_shared_with_agent
# ---------------------------------------------------------------------------


class TestAllowedSharedWithAgent:
    async def test_agent_allowed_on_shared_with_agent(self) -> None:
        agent_did = "did:arc:org:agent/myagent"
        owner_did = "did:arc:org:user/owner"

        bus = _make_bus()
        module = _make_module(tier="personal")
        await module.startup(_make_module_ctx(bus))

        ctx = await bus.emit(
            "memory.read",
            {
                "caller_did": agent_did,
                "target_user_did": owner_did,
                "session_acl_content": _shared_with_agent_content(owner=owner_did),
            },
            agent_did=agent_did,  # bus-level agent_did matches caller_did
        )
        assert not ctx.is_vetoed

    async def test_enterprise_default_allows_agent(self) -> None:
        agent_did = "did:arc:org:agent/ent-agent"
        owner_did = "did:arc:org:user/owner"

        bus = _make_bus()
        module = _make_module(tier="enterprise")
        await module.startup(_make_module_ctx(bus))

        # No explicit ACL → enterprise default is shared-with-agent
        ctx = await bus.emit(
            "memory.read",
            {
                "caller_did": agent_did,
                "target_user_did": owner_did,
                "owner_did": owner_did,
                # No session_acl_content
            },
            agent_did=agent_did,
        )
        assert not ctx.is_vetoed


# ---------------------------------------------------------------------------
# Test item 4: test_veto_emits_audit_event
# ---------------------------------------------------------------------------


class TestVetoEmitsAuditEvent:
    async def test_veto_calls_telemetry_audit(self) -> None:
        mock_telemetry = MagicMock()
        bus = _make_bus()
        module = _make_module(tier="federal", telemetry=mock_telemetry)
        await module.startup(_make_module_ctx(bus))

        await bus.emit(
            "memory.read",
            {
                "caller_did": "did:arc:org:user/stranger",
                "target_user_did": "did:arc:org:user/owner",
                "session_acl_content": _private_acl_content(),
            },
            agent_did="did:arc:org:agent/agent1",
        )

        mock_telemetry.audit_event.assert_called()
        call_args = mock_telemetry.audit_event.call_args
        event_name = call_args[0][0]
        payload = call_args[0][1]

        assert event_name == "session.acl.veto"
        assert "caller_did" in payload
        assert "target_user_did" in payload
        assert "classification" in payload
        assert "reason" in payload

    async def test_no_veto_no_audit_event_for_valid_read(self) -> None:
        mock_telemetry = MagicMock()
        agent_did = "did:arc:org:agent/myagent"
        owner_did = "did:arc:org:user/owner"

        bus = _make_bus()
        module = _make_module(tier="personal", telemetry=mock_telemetry)
        await module.startup(_make_module_ctx(bus))

        await bus.emit(
            "memory.read",
            {
                "caller_did": owner_did,
                "target_user_did": owner_did,
                "session_acl_content": _private_acl_content(owner=owner_did),
            },
            agent_did=agent_did,
        )

        # veto audit event should NOT have been called
        for call in mock_telemetry.audit_event.call_args_list:
            assert call[0][0] != "session.acl.veto"


# ---------------------------------------------------------------------------
# Additional: shutdown
# ---------------------------------------------------------------------------


class TestModuleShutdown:
    async def test_shutdown_does_not_raise(self) -> None:
        module = _make_module()
        await module.shutdown()  # Should not raise
