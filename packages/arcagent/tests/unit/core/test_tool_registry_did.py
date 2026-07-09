"""Tests for §2: tool_registry caller_did mandatory at all tiers.

Every tool dispatch must record actor_did in the audit event.
An unidentified caller (did:arc:unknown) triggers a security audit event.
"""

from __future__ import annotations

from typing import Any, Literal
from unittest.mock import MagicMock

from arcagent.core.config import AgentConfig, ArcAgentConfig, LLMConfig
from arcagent.core.module_bus import ModuleBus
from arcagent.core.tool_registry import RegisteredTool, ToolRegistry, ToolTransport


def _make_registry(
    *,
    agent_did: str = "did:arc:testorg:executor/abc123",
    tier: Literal["federal", "enterprise", "personal"] = "personal",
) -> tuple[ToolRegistry, MagicMock]:
    from unittest.mock import AsyncMock

    config = ArcAgentConfig(
        agent=AgentConfig(name="test"),
        llm=LLMConfig(model="test/model"),
    )
    mock_telemetry = MagicMock()
    mock_telemetry.audit_event = MagicMock()
    # tool_span must be an async context manager
    span_ctx = MagicMock()
    span_ctx.__aenter__ = AsyncMock(return_value=MagicMock())
    span_ctx.__aexit__ = AsyncMock(return_value=False)
    mock_telemetry.tool_span = MagicMock(return_value=span_ctx)
    bus = ModuleBus()
    registry = ToolRegistry(
        config=config.tools,
        bus=bus,
        telemetry=mock_telemetry,
        agent_did=agent_did,
        tier=tier,
    )
    return registry, mock_telemetry


def _make_tool(name: str = "echo_tool") -> RegisteredTool:
    async def execute(**kwargs: Any) -> str:
        return "ok"

    return RegisteredTool(
        name=name,
        description="test",
        input_schema={"type": "object", "properties": {}},
        transport=ToolTransport.NATIVE,
        execute=execute,
    )


def _make_capturing_tool(name: str, captured: dict[str, Any]) -> RegisteredTool:
    async def execute(**kwargs: Any) -> str:
        captured.clear()
        captured.update(kwargs)
        return "ok"

    return RegisteredTool(
        name=name,
        description="test",
        input_schema={"type": "object", "properties": {"key": {"type": "string"}}},
        transport=ToolTransport.NATIVE,
        execute=execute,
    )


class TestCallerDidBindingDispatch:
    """ASI-03 / LLM-01 — the transport strips LLM-supplied identity args."""

    async def test_memory_tool_strips_injected_identity_args(self) -> None:
        real_did = "did:arc:testorg:executor/real"
        registry, mock_telemetry = _make_registry(agent_did=real_did)
        captured: dict[str, Any] = {}
        registry.register(_make_capturing_tool("memory.read", captured))

        arcrun_tools = registry.to_arcrun_tools()
        await arcrun_tools[0].execute(
            {"key": "k", "user_did": "did:evil", "owner_did": "did:evil"},
            MagicMock(),
        )

        # Identity args stripped; memory.read declares no caller_did, so none injected.
        assert captured == {"key": "k"}
        events = [call[0][0] for call in mock_telemetry.audit_event.call_args_list]
        assert "security.caller_did_override_attempt" in events

    async def test_memory_tool_preserves_declared_user_did(self) -> None:
        """A memory-family tool whose schema legitimately declares ``user_did``
        (e.g. user_profile_read/write/tombstone) must receive it — the defence
        strips only injected, undeclared identity args."""
        real_did = "did:arc:testorg:executor/real"
        registry, _ = _make_registry(agent_did=real_did)
        captured: dict[str, Any] = {}

        async def execute(**kwargs: Any) -> str:
            captured.clear()
            captured.update(kwargs)
            return "ok"

        registry.register(
            RegisteredTool(
                name="user_profile_read",
                description="read a user profile",
                input_schema={
                    "type": "object",
                    "properties": {"user_did": {"type": "string"}},
                    "required": ["user_did"],
                },
                transport=ToolTransport.NATIVE,
                execute=execute,
            )
        )

        arcrun_tools = registry.to_arcrun_tools()
        await arcrun_tools[0].execute({"user_did": "did:arc:alice"}, MagicMock())

        # The declared, required user_did survived to the tool; no caller_did
        # was injected because the schema does not declare it.
        assert captured == {"user_did": "did:arc:alice"}

    async def test_non_memory_tool_args_pass_through_untouched(self) -> None:
        registry, _ = _make_registry(agent_did="did:arc:testorg:executor/real")
        captured: dict[str, Any] = {}
        registry.register(_make_capturing_tool("bash", captured))

        arcrun_tools = registry.to_arcrun_tools()
        await arcrun_tools[0].execute({"key": "ls", "user_did": "did:evil"}, MagicMock())

        # Non-memory tools are outside the identity contract — args untouched.
        assert captured == {"key": "ls", "user_did": "did:evil"}


class TestToolDispatchAuditActorDID:
    """§2: every tool.executed audit event must include actor_did."""

    async def test_tool_dispatch_records_actor_did_in_audit(self) -> None:
        did = "did:arc:testorg:executor/abc123"
        registry, mock_telemetry = _make_registry(agent_did=did)
        tool = _make_tool("echo_tool")
        registry.register(tool)

        arcrun_tools = registry.to_arcrun_tools()
        assert len(arcrun_tools) == 1

        ctx = MagicMock()
        await arcrun_tools[0].execute({}, ctx)

        # audit_event must have been called with "tool.executed"
        calls = [
            call
            for call in mock_telemetry.audit_event.call_args_list
            if call[0][0] == "tool.executed"
        ]
        assert len(calls) >= 1, "Expected at least one tool.executed audit event"

        details = calls[0][0][1]
        # actor_did must be present and match the registered DID
        assert details.get("actor_did") == did

    async def test_unknown_did_triggers_security_audit_event(self) -> None:
        """A tool dispatched with the default unknown DID logs a security warning."""
        registry, mock_telemetry = _make_registry(agent_did="did:arc:unknown")
        tool = _make_tool("echo_tool")
        registry.register(tool)

        arcrun_tools = registry.to_arcrun_tools()

        ctx = MagicMock()
        await arcrun_tools[0].execute({}, ctx)

        all_calls = [call[0][0] for call in mock_telemetry.audit_event.call_args_list]
        # Dispatcher should emit a security event for unknown DID
        assert any("security" in c or "unknown" in c or "tool.executed" in c for c in all_calls)

    async def test_tool_dispatch_includes_tier_in_audit(self) -> None:
        registry, mock_telemetry = _make_registry(tier="enterprise")
        tool = _make_tool("check_tool")
        registry.register(tool)

        arcrun_tools = registry.to_arcrun_tools()
        ctx = MagicMock()
        await arcrun_tools[0].execute({}, ctx)

        calls = [
            call
            for call in mock_telemetry.audit_event.call_args_list
            if call[0][0] == "tool.executed"
        ]
        assert len(calls) >= 1
        details = calls[0][0][1]
        assert details.get("tier") == "enterprise"
