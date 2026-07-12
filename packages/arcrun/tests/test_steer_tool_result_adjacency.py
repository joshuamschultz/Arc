"""Regression: a mid-run steer must not orphan a turn's ``tool_use`` block.

Anthropic rejects any request where a ``tool_use`` block is not immediately
followed by its ``tool_result`` block. An always-on agent that receives a
channel/inbox message mid-run queues a steer; if that steer is injected between
the assistant(tool_use) message and its tool_result, the next ``model.invoke``
carries an orphaned tool_use and the provider returns HTTP 400.

The bug window: a steer that arrives AFTER a turn's top-of-loop drain but BEFORE
the post-tool drain lands its user message between the assistant(tool_use) and
the tool_result. These tests pin the invariant that every ``tool_use`` in the
messages handed to the model is immediately followed by its ``tool_result``.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import LLMResponse, Message, ToolCall

from arcrun.events import EventBus
from arcrun.registry import ToolRegistry
from arcrun.sandbox import Sandbox
from arcrun.state import Injection, RunState
from arcrun.strategies.react import react_loop
from arcrun.types import Tool


async def _echo_execute(params: dict, ctx: object) -> str:
    return f"echo: {params.get('input', '')}"


def _echo_tool() -> Tool:
    return Tool(
        name="echo",
        description="Echo input",
        input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
        execute=_echo_execute,
    )


class _SteerMidRunModel:
    """Injects a steer during turn 1's ``invoke`` and snapshots each payload.

    The steer is enqueued from inside the first ``invoke`` — i.e. after that
    turn's top-of-loop drain already ran (queue empty) but before the post-tool
    drain — which is exactly the window an always-on agent hits when a new
    message arrives while a run is executing tools. ``snapshots`` records the
    message list as it would be sent to the provider on each turn.
    """

    def __init__(self, responses: list[LLMResponse], steer_queue: Any, injection: Injection) -> None:
        self._responses = list(responses)
        self._i = 0
        self._steer_queue = steer_queue
        self._injection = injection
        self.snapshots: list[list[Any]] = []

    async def invoke(self, messages: list[Any], tools: list | None = None) -> LLMResponse:
        # Copy the list so the snapshot reflects THIS turn's payload, not the
        # ever-growing live ``state.messages``.
        self.snapshots.append(list(messages))
        resp = self._responses[self._i]
        self._i += 1
        if self._i == 1:
            self._steer_queue.put_nowait(self._injection)
        return resp


def _adjacency_violation(messages: list[Any]) -> str | None:
    """Return the id of a tool_use not immediately followed by its result.

    ``None`` means every tool_use block is immediately followed by a message
    carrying its matching tool_result — the sequence the Anthropic API accepts.
    """
    for i, msg in enumerate(messages):
        content = msg.content
        if not isinstance(content, list):
            continue
        for block in content:
            if getattr(block, "type", None) != "tool_use":
                continue
            nxt = messages[i + 1] if i + 1 < len(messages) else None
            if not _carries_tool_result(nxt, block.id):
                return str(block.id)
    return None


def _carries_tool_result(msg: Any, tool_use_id: str) -> bool:
    if msg is None or not isinstance(msg.content, list):
        return False
    return any(
        getattr(b, "type", None) == "tool_result" and b.tool_use_id == tool_use_id
        for b in msg.content
    )


def _make_state(bus: EventBus) -> RunState:
    reg = ToolRegistry(tools=[_echo_tool()], event_bus=bus)
    messages = [
        Message(role="system", content="You are helpful."),
        Message(role="user", content="Do the task."),
    ]
    return RunState(messages=messages, registry=reg, event_bus=bus, run_id="test-run")


@pytest.mark.asyncio
async def test_mid_run_steer_keeps_tool_use_and_result_adjacent() -> None:
    """A steer arriving mid-tool-execution must not orphan the tool_use."""
    bus = EventBus(run_id="test")
    state = _make_state(bus)
    injection = Injection.new("did:arc:caller", "change direction")
    model = _SteerMidRunModel(
        [
            LLMResponse(
                tool_calls=[ToolCall(id="toolu_1", name="echo", arguments={"input": "a"})],
                stop_reason="tool_use",
            ),
            LLMResponse(content="Steered.", stop_reason="end_turn"),
        ],
        steer_queue=state.steer_queue,
        injection=injection,
    )
    sandbox = Sandbox(config=None, event_bus=bus)

    await react_loop(model, state, sandbox, max_turns=5)

    # Turn 2's payload is the one Anthropic rejected in production.
    assert len(model.snapshots) == 2, "expected a second turn after the tool call"
    orphan = _adjacency_violation(model.snapshots[1])
    assert orphan is None, f"tool_use {orphan!r} sent without an adjacent tool_result"
    # And the steer WAS delivered — the fix relocates it, it does not drop it.
    assert any(
        m.role == "user" and m.content == "change direction" for m in state.messages
    ), "steer message must still be injected, just after the tool_result"


@pytest.mark.asyncio
async def test_final_message_history_has_no_orphaned_tool_use() -> None:
    """The persisted message history keeps every tool_use adjacent to its result."""
    bus = EventBus(run_id="test")
    state = _make_state(bus)
    injection = Injection.new("did:arc:caller", "redirect now")
    model = _SteerMidRunModel(
        [
            LLMResponse(
                tool_calls=[ToolCall(id="toolu_9", name="echo", arguments={"input": "x"})],
                stop_reason="tool_use",
            ),
            LLMResponse(content="Done.", stop_reason="end_turn"),
        ],
        steer_queue=state.steer_queue,
        injection=injection,
    )
    sandbox = Sandbox(config=None, event_bus=bus)

    await react_loop(model, state, sandbox, max_turns=5)

    assert _adjacency_violation(state.messages) is None
