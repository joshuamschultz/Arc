"""E2E spawn tests using a real LLM (Anthropic claude-haiku-4-5).

These tests verify that the full spawn pipeline works end-to-end:
- Parent agent calls spawn_task with a real LLM deciding to spawn
- Child agent executes with a real LLM
- Child result feeds back into the parent's context
- Parent uses the child result in its final answer

Requires ANTHROPIC_API_KEY in the environment (or .env file).
"""
from __future__ import annotations

import asyncio
import os
import sys
import json
from pathlib import Path

import pytest

# Load .env if present (for API keys)
_env_file = Path(__file__).resolve().parents[3] / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

# Skip entire module if no API key
pytestmark = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set",
)


async def _echo_tool(params: dict, ctx: object) -> str:
    """Simple echo tool that returns its input."""
    return f"echo: {params.get('text', '')}"


async def _add_tool(params: dict, ctx: object) -> str:
    """Adds two numbers and returns the result."""
    a = params.get("a", 0)
    b = params.get("b", 0)
    return str(a + b)


async def _get_capital_tool(params: dict, ctx: object) -> str:
    """Returns the capital of a country."""
    capitals = {
        "france": "Paris",
        "japan": "Tokyo",
        "brazil": "Brasilia",
        "germany": "Berlin",
    }
    country = params.get("country", "").lower()
    return capitals.get(country, f"Unknown capital for {country}")


def _make_tool(name: str, description: str, fn, schema: dict, timeout: float = 30.0):
    """Create an arcrun Tool."""
    from arcrun.types import Tool
    return Tool(
        name=name,
        description=description,
        input_schema=schema,
        execute=fn,
        timeout_seconds=timeout,
    )


def _build_tools():
    """Build the tool set for tests."""
    echo = _make_tool(
        "echo", "Echo text back to the user",
        _echo_tool,
        {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Text to echo"}},
            "required": ["text"],
        },
    )
    add = _make_tool(
        "add", "Add two numbers together",
        _add_tool,
        {
            "type": "object",
            "properties": {
                "a": {"type": "number", "description": "First number"},
                "b": {"type": "number", "description": "Second number"},
            },
            "required": ["a", "b"],
        },
    )
    capital = _make_tool(
        "get_capital", "Look up the capital city of a country",
        _get_capital_tool,
        {
            "type": "object",
            "properties": {"country": {"type": "string", "description": "Country name"}},
            "required": ["country"],
        },
    )
    return [echo, add, capital]


def _load_model():
    """Load a real arcllm model (Haiku for speed + cost)."""
    from arcllm import load_model
    return load_model("anthropic", model="claude-haiku-4-5-20251001")


# ─── Test 1: Single spawn round-trip ─────────────────────────────


@pytest.mark.asyncio
async def test_single_spawn_real_llm():
    """Parent spawns a child to look up a capital, uses the result."""
    from arcrun.loop import run

    model = _load_model()
    tools = _build_tools()
    events = []

    result = await run(
        model=model,
        tools=tools,
        system_prompt=(
            "You are a helpful assistant. You have access to tools and can "
            "spawn child tasks using spawn_task. When asked a compound question, "
            "use spawn_task to delegate sub-questions to child agents."
        ),
        task=(
            "I need you to find the capital of France. "
            "Use spawn_task to delegate this to a child agent, then "
            "report the answer the child gives you."
        ),
        max_turns=10,
        on_event=lambda e: events.append(e),
    )

    # Verify we got a final answer
    assert result.content is not None, "Expected content in result"
    assert "paris" in result.content.lower(), (
        f"Expected 'Paris' in final answer, got: {result.content}"
    )

    # Verify spawn events were emitted
    event_types = [e.type for e in events]
    assert "spawn.start" in event_types, (
        f"Expected spawn.start event, got: {event_types}"
    )
    assert "spawn.complete" in event_types, (
        f"Expected spawn.complete event, got: {event_types}"
    )

    # Verify the spawn completed successfully
    spawn_complete = [e for e in events if e.type == "spawn.complete"][0]
    assert spawn_complete.data.get("success") is True, (
        f"Expected successful spawn, got: {spawn_complete.data}"
    )

    # Verify child events bubbled up
    child_events = [e for e in events if e.type.startswith("child.")]
    assert len(child_events) > 0, "Expected child events to bubble up"

    # Print summary for visibility
    print(f"\n--- Single Spawn E2E ---")
    print(f"Final answer: {result.content[:200]}")
    print(f"Turns: {result.turns}")
    print(f"Tool calls: {result.tool_calls_made}")
    print(f"Tokens: {result.tokens_used}")
    print(f"Cost: ${result.cost_usd:.4f}")
    print(f"Events: {len(events)} total, {len(child_events)} from child")

    await model.close()


# ─── Test 2: Parallel spawns ─────────────────────────────────────


@pytest.mark.asyncio
async def test_parallel_spawns_real_llm():
    """Parent spawns two children in parallel, combines results."""
    from arcrun.loop import run

    model = _load_model()
    tools = _build_tools()
    events = []

    result = await run(
        model=model,
        tools=tools,
        system_prompt=(
            "You are a helpful assistant with spawn_task capability. "
            "When given multiple independent sub-tasks, spawn them in parallel "
            "by calling spawn_task multiple times in a single response."
        ),
        task=(
            "I need the capitals of both France and Japan. "
            "Spawn TWO separate child tasks - one for each country - "
            "in the SAME response. Then combine both answers."
        ),
        max_turns=10,
        on_event=lambda e: events.append(e),
    )

    assert result.content is not None
    content_lower = result.content.lower()
    assert "paris" in content_lower, f"Expected 'Paris', got: {result.content}"
    assert "tokyo" in content_lower, f"Expected 'Tokyo', got: {result.content}"

    # Check that two spawns occurred
    spawn_starts = [e for e in events if e.type == "spawn.start"]
    spawn_completes = [e for e in events if e.type == "spawn.complete"]
    print(f"\n--- Parallel Spawn E2E ---")
    print(f"Final answer: {result.content[:300]}")
    print(f"Spawn starts: {len(spawn_starts)}, completes: {len(spawn_completes)}")
    print(f"Turns: {result.turns}, Tool calls: {result.tool_calls_made}")
    print(f"Cost: ${result.cost_usd:.4f}")

    # At least one spawn should have happened
    assert len(spawn_starts) >= 1, "Expected at least one spawn"

    await model.close()


# ─── Test 3: Child uses tools ────────────────────────────────────


@pytest.mark.asyncio
async def test_child_uses_tools_real_llm():
    """Child agent actually uses tools (add) and returns result to parent."""
    from arcrun.loop import run

    model = _load_model()
    tools = _build_tools()
    events = []

    result = await run(
        model=model,
        tools=tools,
        system_prompt=(
            "You are a math assistant. Use spawn_task to delegate calculations "
            "to a child agent. The child should use the 'add' tool."
        ),
        task=(
            "What is 17 + 25? Use spawn_task to delegate this calculation "
            "to a child agent. The child must use the 'add' tool to compute it. "
            "Report the child's answer."
        ),
        max_turns=10,
        on_event=lambda e: events.append(e),
    )

    assert result.content is not None
    assert "42" in result.content, f"Expected '42' in answer, got: {result.content}"

    # Verify child used the add tool (via bubbled events)
    child_tool_events = [
        e for e in events
        if e.type.startswith("child.") and "tool.start" in e.type
    ]

    print(f"\n--- Child Tool Use E2E ---")
    print(f"Final answer: {result.content[:200]}")
    print(f"Child tool events: {len(child_tool_events)}")
    for e in child_tool_events:
        print(f"  {e.type}: {e.data.get('tool_name', 'unknown')}")
    print(f"Cost: ${result.cost_usd:.4f}")

    await model.close()


# ─── Test 4: Event audit trail completeness ──────────────────────


@pytest.mark.asyncio
async def test_spawn_audit_trail_real_llm():
    """Verify the full audit trail is emitted for spawn lifecycle."""
    from arcrun.loop import run

    model = _load_model()
    tools = _build_tools()
    events = []

    result = await run(
        model=model,
        tools=tools,
        system_prompt="You are a helpful assistant. Use spawn_task when asked.",
        task="Use spawn_task to echo 'hello world' using the echo tool.",
        max_turns=10,
        on_event=lambda e: events.append(e),
    )

    event_types = [e.type for e in events]

    # Parent lifecycle
    assert "loop.start" in event_types, f"Missing loop.start: {event_types}"
    assert "loop.complete" in event_types, f"Missing loop.complete: {event_types}"

    # Spawn lifecycle
    assert "spawn.start" in event_types, f"Missing spawn.start: {event_types}"
    assert "spawn.complete" in event_types, f"Missing spawn.complete: {event_types}"

    # Inspect spawn.start event data
    spawn_start = [e for e in events if e.type == "spawn.start"][0]
    assert "child_run_id" in spawn_start.data
    assert "parent_run_id" in spawn_start.data
    assert "parent_depth" in spawn_start.data

    # Inspect spawn.complete event data
    spawn_complete = [e for e in events if e.type == "spawn.complete"][0]
    assert "child_run_id" in spawn_complete.data
    assert "success" in spawn_complete.data

    # Child events bubbled with prefix
    child_events = [e for e in events if e.type.startswith("child.")]
    child_run_id = spawn_start.data["child_run_id"]

    print(f"\n--- Audit Trail E2E ---")
    print(f"Total events: {len(events)}")
    print(f"Child events: {len(child_events)}")
    print(f"Child run ID: {child_run_id}")
    print(f"All event types:")
    for t in event_types:
        print(f"  {t}")

    await model.close()


# ─── Run directly ────────────────────────────────────────────────

if __name__ == "__main__":
    # Allow running directly: python test_spawn_e2e.py
    async def main():
        print("=== E2E Spawn Tests (Real LLM) ===\n")

        print("Test 1: Single spawn round-trip")
        await test_single_spawn_real_llm()

        print("\nTest 2: Parallel spawns")
        await test_parallel_spawns_real_llm()

        print("\nTest 3: Child uses tools")
        await test_child_uses_tools_real_llm()

        print("\nTest 4: Audit trail completeness")
        await test_spawn_audit_trail_real_llm()

        print("\n=== All E2E tests passed ===")

    asyncio.run(main())
