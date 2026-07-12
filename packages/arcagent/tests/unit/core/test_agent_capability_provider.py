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


# --- U13: a tool that declares requires_skill activates it on invoke ----------


def _audit_sink() -> tuple[list[tuple[str, dict[str, Any]]], Any]:
    """A capturing telemetry.audit_event stand-in."""
    events: list[tuple[str, dict[str, Any]]] = []

    def _emit(event_type: str, details: dict[str, Any]) -> None:
        events.append((event_type, details))

    return events, _emit


@pytest.mark.asyncio
async def test_invoke_activates_required_skill(tmp_path: Path) -> None:
    """A tool with requires_skill pulls that skill's body into the result (U13)."""
    events, sink = _audit_sink()
    provider = AgentCapabilityProvider(
        tools=[_tool("create_skill")],
        skills=[_skill("create-skill", tmp_path, body="STEP 1: author SKILL.md")],
        tier="personal",
        caller_did="did:arc:agent",
        requires_skill={"create_skill": "create-skill"},
        audit=sink,
    )
    result = await provider.invoke("create_skill", {"x": "hi"}, caller_did="did:arc:agent")
    # The skill body is now IN the result content (activated via the tool call).
    assert "STEP 1: author SKILL.md" in result.content
    assert "ran create_skill" in result.content
    assert result.is_error is False
    # The generic passthrough carries the signal arcrun spools onto the tool_event.
    assert result.extra == {"activated_skill": "create-skill", "skill_activated": True}
    # Exactly one skill-activation audit event, marked activated.
    activations = [d for e, d in events if e == "tool.skill_activated"]
    assert len(activations) == 1
    assert activations[0]["requires_skill"] == "create-skill"
    assert activations[0]["skill_activated"] is True
    assert activations[0]["tool"] == "create_skill"


@pytest.mark.asyncio
async def test_required_skill_activated_once_per_run(tmp_path: Path) -> None:
    """The skill body is injected once per run; later calls don't re-inject it."""
    events, sink = _audit_sink()
    provider = AgentCapabilityProvider(
        tools=[_tool("create_skill")],
        skills=[_skill("create-skill", tmp_path, body="RUNBOOK")],
        tier="personal",
        caller_did="did:arc:agent",
        requires_skill={"create_skill": "create-skill"},
        audit=sink,
    )
    first = await provider.invoke("create_skill", {}, caller_did="did:arc:agent")
    assert "RUNBOOK" in first.content
    second = await provider.invoke("create_skill", {}, caller_did="did:arc:agent")
    # Already active — body not re-injected, but still audited as active.
    assert "RUNBOOK" not in second.content
    activations = [d for e, d in events if e == "tool.skill_activated"]
    assert len(activations) == 2
    assert activations[1]["already_active"] is True
    assert activations[1]["skill_activated"] is True


@pytest.mark.asyncio
async def test_missing_required_skill_fails_open(tmp_path: Path) -> None:
    """A required skill that can't be loaded doesn't crash the tool call (fail-open)."""
    events, sink = _audit_sink()
    provider = AgentCapabilityProvider(
        tools=[_tool("create_skill")],
        skills=[],  # the required skill is absent
        tier="personal",
        caller_did="did:arc:agent",
        requires_skill={"create_skill": "ghost-skill"},
        audit=sink,
    )
    result = await provider.invoke("create_skill", {}, caller_did="did:arc:agent")
    assert result.is_error is False
    assert "ran create_skill" in result.content
    assert result.extra == {"activated_skill": "ghost-skill", "skill_activated": False}
    activations = [d for e, d in events if e == "tool.skill_activated"]
    assert len(activations) == 1
    assert activations[0]["skill_activated"] is False


@pytest.mark.asyncio
async def test_tool_without_requires_skill_is_untouched(tmp_path: Path) -> None:
    """No requires_skill → result content and audit are unchanged."""
    events, sink = _audit_sink()
    provider = AgentCapabilityProvider(
        tools=[_tool("search")],
        skills=[_skill("create-skill", tmp_path, body="RUNBOOK")],
        tier="personal",
        caller_did="did:arc:agent",
        requires_skill={"create_skill": "create-skill"},
        audit=sink,
    )
    result = await provider.invoke("search", {"x": "hi"}, caller_did="did:arc:agent")
    assert result.content == "ran search({'x': 'hi'})"
    assert result.extra is None
    assert [e for e, _ in events if e == "tool.skill_activated"] == []
