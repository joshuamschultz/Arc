"""Phase C (SPEC-027 / ADR-023) — AgentCapabilityProvider.

The agent implements arcrun's CapabilityProvider over its tools + skills:
- advertise() is lean (name/kind/schema, no skill bodies) — AC-4.1
- load() fetches a skill body only on demand — AC-4.2
- invoke() routes through the tool's policy-wrapped execute, failing closed — AC-4.4
- federal tier denies workspace-authored capabilities at load — AC-6.1
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arcrun import CapabilityProvider, Tool

from arcagent.capabilities.provider import AgentCapabilityProvider, _Skill


def _tool(name: str, *, raises: bool = False, denied: bool = False) -> Tool:
    async def _execute(args: dict[str, Any], ctx: Any) -> str:
        if raises:
            raise RuntimeError("boom")
        if denied:
            # Mirrors the core ToolRegistry's policy wrapper, which returns a
            # denial string rather than executing.
            return "Error: tool denied — policy DENY"
        return f"ran {name}({args})"

    return Tool(
        name=name,
        description=f"the {name} tool",
        input_schema={"type": "object", "properties": {"x": {"type": "string"}}},
        execute=_execute,
    )


def _skill(name: str, tmp_path: Path, *, scan_root: str = "agent", body: str = "BODY") -> _Skill:
    md = tmp_path / f"{name}.md"
    md.write_text(body, encoding="utf-8")
    return _Skill(name=name, description=f"use {name} when X", location=md, scan_root=scan_root)


def test_provider_satisfies_protocol() -> None:
    assert isinstance(
        AgentCapabilityProvider(tools=[], skills=[], tier="personal", caller_did="did:arc:a"),
        CapabilityProvider,
    )


def test_advertise_is_lean(tmp_path: Path) -> None:
    """advertise() carries tool schemas + skill names, but never skill bodies (AC-4.1)."""
    provider = AgentCapabilityProvider(
        tools=[_tool("search")],
        skills=[_skill("deploy", tmp_path, body="SECRET RUNBOOK STEPS")],
        tier="personal",
        caller_did="did:arc:agent",
    )
    specs = {s.name: s for s in provider.advertise()}

    assert specs["search"].kind == "tool"
    assert specs["search"].input_schema["properties"] == {"x": {"type": "string"}}
    assert specs["deploy"].kind == "skill"
    # The lean manifest must not contain the skill body.
    assert all("SECRET RUNBOOK" not in s.description for s in specs.values())


@pytest.mark.asyncio
async def test_load_fetches_skill_body_on_demand(tmp_path: Path) -> None:
    """The skill body enters context only via load() (AC-4.2)."""
    provider = AgentCapabilityProvider(
        tools=[],
        skills=[_skill("deploy", tmp_path, body="STEP 1: drain. STEP 2: ship.")],
        tier="personal",
        caller_did="did:arc:agent",
    )
    body = await provider.load("deploy", caller_did="did:arc:agent")
    assert body == "STEP 1: drain. STEP 2: ship."
    assert await provider.load("nonexistent", caller_did="did:arc:agent") is None


@pytest.mark.asyncio
async def test_invoke_routes_through_execute(tmp_path: Path) -> None:
    """invoke() dispatches the tool's execute and returns its result."""
    provider = AgentCapabilityProvider(
        tools=[_tool("search")], skills=[], tier="personal", caller_did="did:arc:agent"
    )
    result = await provider.invoke("search", {"x": "hi"}, caller_did="did:arc:agent")
    assert result.is_error is False
    assert "ran search" in result.content


@pytest.mark.asyncio
async def test_invoke_catches_a_raising_tool(tmp_path: Path) -> None:
    """A tool that raises mid-invoke is surfaced as an error result, not a crash."""
    provider = AgentCapabilityProvider(
        tools=[_tool("boom", raises=True)],
        skills=[],
        tier="personal",
        caller_did="did:arc:agent",
    )
    result = await provider.invoke("boom", {}, caller_did="did:arc:agent")
    assert result.is_error is True
    assert "RuntimeError" in result.content


@pytest.mark.asyncio
async def test_denied_fails_closed(tmp_path: Path) -> None:
    """A policy-denied tool surfaces the denial; an unknown tool errors closed (AC-4.4)."""
    provider = AgentCapabilityProvider(
        tools=[_tool("danger", denied=True)],
        skills=[],
        tier="personal",
        caller_did="did:arc:agent",
    )
    denied = await provider.invoke("danger", {}, caller_did="did:arc:agent")
    assert "denied" in denied.content

    missing = await provider.invoke("ghost", {}, caller_did="did:arc:agent")
    assert missing.is_error is True


@pytest.mark.asyncio
async def test_federal_gate_denies_workspace_capabilities(tmp_path: Path) -> None:
    """In federal tier, workspace-authored caps are not advertised or loadable (AC-6.1)."""
    workspace_skill = _skill("ws_skill", tmp_path, scan_root="workspace")
    agent_skill = _skill("agent_skill", tmp_path, scan_root="agent")

    federal = AgentCapabilityProvider(
        tools=[_tool("ws_tool"), _tool("builtin_tool")],
        skills=[workspace_skill, agent_skill],
        tier="federal",
        caller_did="did:arc:agent",
        workspace_authored=frozenset({"ws_tool", "ws_skill"}),
    )
    names = {s.name for s in federal.advertise()}
    assert "ws_tool" not in names, "workspace tool must be gated in federal"
    assert "ws_skill" not in names, "workspace skill must be gated in federal"
    assert "builtin_tool" in names
    assert "agent_skill" in names
    # Gated workspace skill is not loadable either (fail closed).
    assert await federal.load("ws_skill", caller_did="did:arc:agent") is None
    # Gated workspace tool is not invocable.
    gated = await federal.invoke("ws_tool", {}, caller_did="did:arc:agent")
    assert gated.is_error is True

    # Personal tier allows the same workspace capabilities (with audit, not gated).
    personal = AgentCapabilityProvider(
        tools=[_tool("ws_tool")],
        skills=[workspace_skill],
        tier="personal",
        caller_did="did:arc:agent",
        workspace_authored=frozenset({"ws_tool", "ws_skill"}),
    )
    personal_names = {s.name for s in personal.advertise()}
    assert "ws_tool" in personal_names
    assert "ws_skill" in personal_names
