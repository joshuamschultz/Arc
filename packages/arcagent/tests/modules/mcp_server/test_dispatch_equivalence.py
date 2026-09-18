"""SPEC-082 T-1081 (RED) — the door dispatches through the ONE existing envelope.

REQ-411, REQ-416 / COMP-003. A verified inbound ``tools/call`` must be turned into
``AgentCapabilityProvider.invoke(name, args, caller_did=<external DID>)`` — the same
envelope a native call uses — so policy (``PolicyPipeline``, first-DENY, fail-closed)
and audit run identically and are never re-implemented. A second dispatch path is the
exact producers-unwired failure mode this requirement forbids.

This test drives ``arcagent.modules.mcp_server.dispatch.dispatch_tools_call``, which
does not exist yet. The RED is the import: ``No module named
'arcagent.modules.mcp_server.dispatch'``. It goes GREEN when T-1082 adds the bridge.

The equivalence is proven two ways:
1. The door's result for ``(verb, args, caller_did)`` equals the result of calling
   ``AgentCapabilityProvider.invoke`` directly with the same triple.
2. The door reaches the provider through ``invoke`` and nowhere else — a recording
   provider proves ``invoke`` was called exactly once, with the external DID bound as
   ``caller_did``. If the door had a private second path, the recorder would be empty.
"""

from __future__ import annotations

from typing import Any

import arcrun
import pytest
from arcrun import Tool

from arcagent.capabilities.provider import AgentCapabilityProvider

_EXTERNAL_DID = "did:arc:acme:exec/deadbeef"


def _tool(name: str, *, denied: bool = False) -> Tool:
    async def _execute(args: dict[str, Any], ctx: Any) -> str:
        if denied:
            # Mirrors the core ToolRegistry's policy wrapper returning a denial string.
            return "Error: tool denied — policy DENY"
        return f"ran {name}({args})"

    return Tool(
        name=name,
        description=f"the {name} tool",
        input_schema={"type": "object", "properties": {"x": {"type": "string"}}},
        execute=_execute,
    )


class _RecordingProvider(AgentCapabilityProvider):
    """An ``AgentCapabilityProvider`` that records every ``invoke`` call.

    The subclass proves the door routes through the one public envelope: a call that
    reaches ``invoke`` is recorded; a private second path would leave ``calls`` empty.
    """

    def __init__(self, tools: list[Tool]) -> None:
        super().__init__(tools=tools, skills=[], tier="personal", caller_did=_EXTERNAL_DID)
        self.calls: list[tuple[str, dict[str, Any], str]] = []

    async def invoke(
        self, name: str, args: dict[str, Any], *, caller_did: str
    ) -> arcrun.CapabilityResult:
        self.calls.append((name, dict(args), caller_did))
        return await super().invoke(name, args, caller_did=caller_did)


@pytest.mark.asyncio
async def test_door_call_matches_the_native_invoke_result() -> None:
    """The door's ``tools/call`` result equals the equivalent direct ``invoke``."""
    from arcagent.modules.mcp_server.dispatch import dispatch_tools_call

    args = {"x": "hi"}
    native_provider = AgentCapabilityProvider(
        tools=[_tool("search")], skills=[], tier="personal", caller_did=_EXTERNAL_DID
    )
    native = await native_provider.invoke("search", args, caller_did=_EXTERNAL_DID)

    door_provider = AgentCapabilityProvider(
        tools=[_tool("search")], skills=[], tier="personal", caller_did=_EXTERNAL_DID
    )
    door = await dispatch_tools_call(
        door_provider, name="search", arguments=args, caller_did=_EXTERNAL_DID
    )

    assert door.content == native.content
    assert door.is_error == native.is_error


@pytest.mark.asyncio
async def test_door_routes_through_invoke_exactly_once() -> None:
    """The door reaches the provider only via ``invoke``, binding the external DID."""
    from arcagent.modules.mcp_server.dispatch import dispatch_tools_call

    provider = _RecordingProvider([_tool("search")])
    args = {"x": "hi"}

    await dispatch_tools_call(provider, name="search", arguments=args, caller_did=_EXTERNAL_DID)

    assert provider.calls == [("search", args, _EXTERNAL_DID)]


@pytest.mark.asyncio
async def test_door_preserves_a_policy_denial_verdict() -> None:
    """A denied capability fails closed through the door exactly as natively (REQ-411)."""
    from arcagent.modules.mcp_server.dispatch import dispatch_tools_call

    native_provider = AgentCapabilityProvider(
        tools=[_tool("danger", denied=True)], skills=[], tier="personal", caller_did=_EXTERNAL_DID
    )
    native = await native_provider.invoke("danger", {}, caller_did=_EXTERNAL_DID)

    door_provider = AgentCapabilityProvider(
        tools=[_tool("danger", denied=True)], skills=[], tier="personal", caller_did=_EXTERNAL_DID
    )
    door = await dispatch_tools_call(
        door_provider, name="danger", arguments={}, caller_did=_EXTERNAL_DID
    )

    assert "denied" in door.content
    assert door.content == native.content
