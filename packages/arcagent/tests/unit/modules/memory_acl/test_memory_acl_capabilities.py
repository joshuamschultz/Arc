"""Unit tests for the memory_acl decorator hooks (the live ACL gate).

The live gate is the three ``@hook`` functions in
``arcagent.modules.memory_acl.capabilities`` (memory.read / write / search
at priority 10), reading shared state from ``memory_acl._runtime``.

Covers:
1. Hooks register at priority 10 via CapabilityLoader.
2. Unauthorized cross-session reads/searches/writes are vetoed.
3. Reads allowed within shared-with-agent / tier defaults.
4. Vetoes emit session.acl.veto audit events; valid reads do not.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from arcagent.capabilities.capability_loader import CapabilityLoader
from arcagent.capabilities.capability_registry import CapabilityRegistry
from arcagent.core.module_bus import EventContext
from arcagent.modules.memory_acl import _runtime
from arcagent.modules.memory_acl.capabilities import (
    memory_acl_read,
    memory_acl_search,
    memory_acl_write,
)


@pytest.fixture(autouse=True)
def _reset_runtime() -> None:
    _runtime.reset()


def _configure(tier: str = "federal", telemetry: Any = None) -> None:
    _runtime.configure(config={"tier": tier}, telemetry=telemetry)


def _ctx(event: str, data: dict[str, Any], agent_did: str) -> EventContext:
    return EventContext(event=event, data=data, agent_did=agent_did, trace_id="trace-test")


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
# Registration at priority 10
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestPriority10:
    async def test_three_hooks_register_at_priority_10(self) -> None:
        from arcagent.modules.memory_acl import capabilities as macl_caps

        module_dir = Path(macl_caps.__file__).parent
        reg = CapabilityRegistry()
        loader = CapabilityLoader(scan_roots=[("memory_acl", module_dir)], registry=reg)
        await loader.scan_and_register()

        for event, name in (
            ("memory.read", "memory_acl_read"),
            ("memory.write", "memory_acl_write"),
            ("memory.search", "memory_acl_search"),
        ):
            hooks = await reg.get_hooks(event)
            matched = [h for h in hooks if h.meta.name == name]
            assert matched, f"No hook {name} for {event}"
            assert all(h.meta.priority == 10 for h in matched)


# ---------------------------------------------------------------------------
# Veto on unauthorized access
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestVetoUnauthorizedRead:
    async def test_private_acl_blocks_stranger(self) -> None:
        _configure(tier="federal")
        ctx = _ctx(
            "memory.read",
            {
                "caller_did": "did:arc:org:user/stranger",
                "target_user_did": "did:arc:org:user/owner",
                "session_acl_content": _private_acl_content(),
            },
            agent_did="did:arc:org:agent/agent1",
        )
        await memory_acl_read(ctx)
        assert ctx.is_vetoed
        assert "ACL denied" in ctx.veto_reason
        assert "private" in ctx.veto_reason

    async def test_no_acl_federal_defaults_to_private(self) -> None:
        _configure(tier="federal")
        ctx = _ctx(
            "memory.read",
            {
                "caller_did": "did:arc:org:user/stranger",
                "target_user_did": "did:arc:org:user/owner",
            },
            agent_did="did:arc:org:agent/agent1",
        )
        await memory_acl_read(ctx)
        assert ctx.is_vetoed

    async def test_same_user_not_vetoed(self) -> None:
        _configure(tier="federal")
        same_did = "did:arc:org:user/same"
        ctx = _ctx(
            "memory.read",
            {
                "caller_did": same_did,
                "target_user_did": same_did,
                "session_acl_content": _private_acl_content(owner=same_did),
            },
            agent_did="did:arc:org:agent/agent1",
        )
        await memory_acl_read(ctx)
        assert not ctx.is_vetoed

    async def test_write_vetoed_for_non_owner(self) -> None:
        _configure(tier="personal")
        ctx = _ctx(
            "memory.write",
            {
                "caller_did": "did:arc:org:user/stranger",
                "target_user_did": "did:arc:org:user/owner",
                "session_acl_content": _shared_with_agent_content(),
            },
            agent_did="did:arc:org:agent/agent1",
        )
        await memory_acl_write(ctx)
        assert ctx.is_vetoed

    async def test_search_vetoed_for_private_session(self) -> None:
        _configure(tier="federal")
        ctx = _ctx(
            "memory.search",
            {
                "caller_did": "did:arc:org:user/stranger",
                "target_user_did": "did:arc:org:user/owner",
                "session_acl_content": _private_acl_content(),
            },
            agent_did="did:arc:org:agent/agent1",
        )
        await memory_acl_search(ctx)
        assert ctx.is_vetoed


# ---------------------------------------------------------------------------
# Allowed within shared-with-agent / tier defaults
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestAllowedSharedWithAgent:
    async def test_agent_allowed_on_shared_with_agent(self) -> None:
        agent_did = "did:arc:org:agent/myagent"
        owner_did = "did:arc:org:user/owner"
        _configure(tier="personal")
        ctx = _ctx(
            "memory.read",
            {
                "caller_did": agent_did,
                "target_user_did": owner_did,
                "session_acl_content": _shared_with_agent_content(owner=owner_did),
            },
            agent_did=agent_did,
        )
        await memory_acl_read(ctx)
        assert not ctx.is_vetoed

    async def test_enterprise_default_allows_agent(self) -> None:
        agent_did = "did:arc:org:agent/ent-agent"
        owner_did = "did:arc:org:user/owner"
        _configure(tier="enterprise")
        ctx = _ctx(
            "memory.read",
            {
                "caller_did": agent_did,
                "target_user_did": owner_did,
                "owner_did": owner_did,
            },
            agent_did=agent_did,
        )
        await memory_acl_read(ctx)
        assert not ctx.is_vetoed


# ---------------------------------------------------------------------------
# Audit emission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestVetoEmitsAuditEvent:
    async def test_veto_calls_telemetry_audit(self) -> None:
        telemetry = MagicMock()
        _configure(tier="federal", telemetry=telemetry)
        ctx = _ctx(
            "memory.read",
            {
                "caller_did": "did:arc:org:user/stranger",
                "target_user_did": "did:arc:org:user/owner",
                "session_acl_content": _private_acl_content(),
            },
            agent_did="did:arc:org:agent/agent1",
        )
        await memory_acl_read(ctx)

        telemetry.audit_event.assert_called()
        event_name, payload = telemetry.audit_event.call_args[0][0], telemetry.audit_event.call_args[0][1]
        assert event_name == "session.acl.veto"
        assert "caller_did" in payload
        assert "target_user_did" in payload
        assert "classification" in payload
        assert "reason" in payload

    async def test_no_veto_no_veto_audit_for_valid_read(self) -> None:
        telemetry = MagicMock()
        agent_did = "did:arc:org:agent/myagent"
        owner_did = "did:arc:org:user/owner"
        _configure(tier="personal", telemetry=telemetry)
        ctx = _ctx(
            "memory.read",
            {
                "caller_did": owner_did,
                "target_user_did": owner_did,
                "session_acl_content": _private_acl_content(owner=owner_did),
            },
            agent_did=agent_did,
        )
        await memory_acl_read(ctx)

        for call in telemetry.audit_event.call_args_list:
            assert call[0][0] != "session.acl.veto"


# ---------------------------------------------------------------------------
# Runtime contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRuntimeContract:
    async def test_unconfigured_raises(self) -> None:
        ctx = _ctx(
            "memory.read",
            {"caller_did": "a", "target_user_did": "b"},
            agent_did="did:arc:org:agent/agent1",
        )
        with pytest.raises(RuntimeError, match="before runtime is configured"):
            await memory_acl_read(ctx)
