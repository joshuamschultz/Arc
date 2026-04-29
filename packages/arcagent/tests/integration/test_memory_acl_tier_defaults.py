"""Integration tests: tier-driven ACL defaults.

Test contract item 5:
- federal → private (cross-session reads blocked unless explicitly shared)
- enterprise → shared-with-agent (within team)
- personal → shared-with-agent
"""

from __future__ import annotations

from unittest.mock import MagicMock

from arcagent.core.module_bus import ModuleBus, ModuleContext
from arcagent.modules.memory_acl.memory_acl_module import MemoryACLModule

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_module_ctx(bus: ModuleBus) -> ModuleContext:
    return ModuleContext(
        bus=bus,
        tool_registry=MagicMock(),
        config=MagicMock(),
        telemetry=MagicMock(),
        workspace=MagicMock(),
        llm_config=MagicMock(),
    )


async def _emit_cross_session_read(
    bus: ModuleBus,
    *,
    caller_did: str,
    target_user_did: str,
    agent_did: str,
    acl_content: str | None = None,
) -> bool:
    """Emit memory.read and return True if vetoed."""
    data: dict = {
        "caller_did": caller_did,
        "target_user_did": target_user_did,
    }
    if acl_content is not None:
        data["session_acl_content"] = acl_content
    ctx = await bus.emit("memory.read", data, agent_did=agent_did)
    return ctx.is_vetoed


# ---------------------------------------------------------------------------
# Federal tier: default = private
# ---------------------------------------------------------------------------


class TestFederalTierDefaults:
    async def test_federal_no_acl_cross_session_read_vetoed(self) -> None:
        """Test item 5: federal tier without ACL blocks cross-session reads."""
        stranger = "did:arc:org:user/stranger"
        owner = "did:arc:org:user/owner"
        agent = "did:arc:org:agent/agent1"

        bus = ModuleBus()
        module = MemoryACLModule(config={"tier": "federal"})
        await module.startup(_make_module_ctx(bus))

        vetoed = await _emit_cross_session_read(
            bus,
            caller_did=stranger,
            target_user_did=owner,
            agent_did=agent,
            # No ACL content → defaults to federal private
        )
        assert vetoed, "Federal tier without ACL must block cross-session reads"

    async def test_federal_explicit_private_acl_blocks_cross_session(self) -> None:
        owner = "did:arc:org:user/owner"
        agent = "did:arc:org:agent/agent1"
        stranger = "did:arc:org:user/stranger"

        content = f"""---
acl:
  cross_session_visibility: private
owner_did: {owner}
---
"""
        bus = ModuleBus()
        module = MemoryACLModule(config={"tier": "federal"})
        await module.startup(_make_module_ctx(bus))

        vetoed = await _emit_cross_session_read(
            bus,
            caller_did=stranger,
            target_user_did=owner,
            agent_did=agent,
            acl_content=content,
        )
        assert vetoed

    async def test_federal_owner_reads_own_session_allowed(self) -> None:
        """Owner reading their own federal session must succeed."""
        owner = "did:arc:org:user/owner"
        agent = "did:arc:org:agent/agent1"

        bus = ModuleBus()
        module = MemoryACLModule(config={"tier": "federal"})
        await module.startup(_make_module_ctx(bus))

        vetoed = await _emit_cross_session_read(
            bus,
            caller_did=owner,
            target_user_did=owner,
            agent_did=agent,
        )
        assert not vetoed

    async def test_federal_explicit_shared_acl_allows_agent(self) -> None:
        """Even in federal tier, an explicit shared-with-agent ACL allows agent."""
        owner = "did:arc:org:user/owner"
        agent = "did:arc:org:agent/agent1"

        content = f"""---
acl:
  cross_session_visibility: shared-with-agent
owner_did: {owner}
---
"""
        bus = ModuleBus()
        module = MemoryACLModule(config={"tier": "federal"})
        await module.startup(_make_module_ctx(bus))

        vetoed = await _emit_cross_session_read(
            bus,
            caller_did=agent,
            target_user_did=owner,
            agent_did=agent,
            acl_content=content,
        )
        assert not vetoed


# ---------------------------------------------------------------------------
# Enterprise tier: default = shared-with-agent
# ---------------------------------------------------------------------------


class TestEnterpriseTierDefaults:
    async def test_enterprise_no_acl_agent_allowed(self) -> None:
        """Enterprise default: agent may read shared-with-agent sessions."""
        owner = "did:arc:org:user/owner"
        agent = "did:arc:org:agent/ent-agent"

        bus = ModuleBus()
        module = MemoryACLModule(config={"tier": "enterprise"})
        await module.startup(_make_module_ctx(bus))

        vetoed = await _emit_cross_session_read(
            bus,
            caller_did=agent,
            target_user_did=owner,
            agent_did=agent,
        )
        assert not vetoed, "Enterprise tier default allows agent reads"

    async def test_enterprise_no_acl_stranger_still_blocked(self) -> None:
        """Enterprise shared-with-agent does NOT mean strangers can read."""
        owner = "did:arc:org:user/owner"
        stranger = "did:arc:org:user/stranger"
        agent = "did:arc:org:agent/ent-agent"

        bus = ModuleBus()
        module = MemoryACLModule(config={"tier": "enterprise"})
        await module.startup(_make_module_ctx(bus))

        vetoed = await _emit_cross_session_read(
            bus,
            caller_did=stranger,
            target_user_did=owner,
            agent_did=agent,
        )
        assert vetoed, "Strangers (non-agent non-owner) must still be blocked"


# ---------------------------------------------------------------------------
# Personal tier: default = shared-with-agent
# ---------------------------------------------------------------------------


class TestPersonalTierDefaults:
    async def test_personal_no_acl_agent_allowed(self) -> None:
        owner = "did:arc:org:user/owner"
        agent = "did:arc:org:agent/personal-agent"

        bus = ModuleBus()
        module = MemoryACLModule(config={"tier": "personal"})
        await module.startup(_make_module_ctx(bus))

        vetoed = await _emit_cross_session_read(
            bus,
            caller_did=agent,
            target_user_did=owner,
            agent_did=agent,
        )
        assert not vetoed

    async def test_personal_stranger_blocked_even_on_shared_with_agent(self) -> None:
        """shared-with-agent means the AGENT, not any user."""
        owner = "did:arc:org:user/owner"
        stranger = "did:arc:org:user/stranger"
        agent = "did:arc:org:agent/personal-agent"

        bus = ModuleBus()
        module = MemoryACLModule(config={"tier": "personal"})
        await module.startup(_make_module_ctx(bus))

        vetoed = await _emit_cross_session_read(
            bus,
            caller_did=stranger,
            target_user_did=owner,
            agent_did=agent,
        )
        assert vetoed
