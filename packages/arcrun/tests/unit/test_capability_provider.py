"""Phase C (SPEC-027 / ADR-023) — arcrun.run takes a CapabilityProvider.

The loop advertises the provider's lean specs to the model and routes every
tool call through ``provider.invoke`` (carrying caller_did). A skill body is
pulled only when the model calls the built-in ``use_skill`` meta-tool, which
routes to ``provider.load``.
"""

from __future__ import annotations

from typing import Any

import pytest

from arcrun import (
    CapabilityProvider,
    CapabilityResult,
    CapabilitySpec,
    StaticProvider,
    Tool,
    provider_tools,
)


class _FakeProvider:
    """In-memory provider: one tool, one lazy skill. Records invoke/load calls."""

    def __init__(self) -> None:
        self.invoked: list[tuple[str, dict[str, Any], str]] = []
        self.loaded: list[tuple[str, str]] = []

    def advertise(self) -> list[CapabilitySpec]:
        return [
            CapabilitySpec(
                name="echo",
                description="Echo the text back.",
                input_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                kind="tool",
            ),
            CapabilitySpec(
                name="deploy_runbook",
                description="Use when deploying to production.",
                input_schema={"type": "object", "properties": {}},
                kind="skill",
            ),
        ]

    async def load(self, name: str, *, caller_did: str) -> str | None:
        self.loaded.append((name, caller_did))
        if name == "deploy_runbook":
            return "STEP 1: drain traffic. STEP 2: ship. STEP 3: verify."
        return None

    async def invoke(self, name: str, args: dict[str, Any], *, caller_did: str) -> CapabilityResult:
        self.invoked.append((name, args, caller_did))
        if name == "echo":
            return CapabilityResult(content=f"echo: {args.get('text', '')}")
        return CapabilityResult(content=f"unknown tool {name}", is_error=True)


def test_fake_provider_satisfies_protocol() -> None:
    assert isinstance(_FakeProvider(), CapabilityProvider)
    assert isinstance(StaticProvider([]), CapabilityProvider)


@pytest.mark.asyncio
async def test_provider_drives_advertise_and_invoke() -> None:
    """provider_tools advertises the tool schema and dispatches invoke with caller_did."""
    provider = _FakeProvider()
    tools = provider_tools(provider, caller_did="did:arc:user:alice")

    by_name = {t.name: t for t in tools}
    # The plain tool is directly invocable; the skill is folded into use_skill.
    assert "echo" in by_name
    assert "deploy_runbook" not in by_name
    assert "use_skill" in by_name
    # The skill is advertised lean in the use_skill menu (no body).
    assert "deploy_runbook" in by_name["use_skill"].description
    assert "STEP 1" not in by_name["use_skill"].description

    # Invoking the tool routes to provider.invoke carrying caller_did.
    from arcrun.capabilities import detached_context

    out = await by_name["echo"].execute({"text": "hi"}, detached_context())
    assert out == "echo: hi"
    assert provider.invoked == [("echo", {"text": "hi"}, "did:arc:user:alice")]


@pytest.mark.asyncio
async def test_use_skill_loads_body_lazily() -> None:
    """The skill body enters context only when use_skill is called (ADR-023 AC-4.2)."""
    provider = _FakeProvider()
    tools = provider_tools(provider, caller_did="did:arc:agent:x")
    use_skill = next(t for t in tools if t.name == "use_skill")

    from arcrun.capabilities import detached_context

    # Unused skill never loaded.
    assert provider.loaded == []

    body = await use_skill.execute({"name": "deploy_runbook"}, detached_context())
    assert "STEP 1" in body
    assert provider.loaded == [("deploy_runbook", "did:arc:agent:x")]


@pytest.mark.asyncio
async def test_static_provider_wraps_a_tool_list() -> None:
    """StaticProvider adapts a fixed tool list — advertise + invoke round-trip."""

    async def _greet(args: dict[str, Any], ctx: Any) -> str:
        return f"hello {args['who']}"

    tool = Tool(
        name="greet",
        description="Greet someone.",
        input_schema={"type": "object", "properties": {"who": {"type": "string"}}},
        execute=_greet,
    )
    provider = StaticProvider([tool])

    specs = provider.advertise()
    assert [s.name for s in specs] == ["greet"]
    result = await provider.invoke("greet", {"who": "world"}, caller_did="did:arc:test")
    assert result.content == "hello world"
    assert result.is_error is False

    missing = await provider.invoke("nope", {}, caller_did="did:arc:test")
    assert missing.is_error is True
