"""Streaming runtime API for arcrun.

Provides run_stream() which wraps run() and yields typed StreamEvent objects
for per-token streaming, tool lifecycle events, and turn completion.

Design:
- run_stream() runs the loop in a background task.
- An on_event bridge converts EventBus events to StreamEvent subclasses.
- Token text is derived from LoopResult.content split into words to simulate
  per-token streaming without requiring model-level streaming support.
- TurnEndEvent is always the final event in the stream.

This module is pure arcrun — no LLM calls, no agent state.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from arcrun.events import Event
from arcrun.types import LoopResult, SandboxConfig, Tool

# ---------------------------------------------------------------------------
# StreamEvent hierarchy
# ---------------------------------------------------------------------------


@dataclass
class StreamEvent:
    """Base class for all stream events."""


@dataclass
class TokenEvent(StreamEvent):
    """A chunk of text from the model response.

    Attributes:
        text: A fragment of the final response text.
    """

    text: str


@dataclass
class ToolStartEvent(StreamEvent):
    """Emitted when the model calls a tool.

    Attributes:
        name: Tool name being invoked.
        args: Arguments passed to the tool.
    """

    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolEndEvent(StreamEvent):
    """Emitted when a tool call completes.

    Attributes:
        result: The tool's return value.
    """

    result: str = ""


@dataclass
class TurnEndEvent(StreamEvent):
    """Emitted exactly once, as the final event in the stream.

    Attributes:
        final_text: The complete response text from the loop.
        turns: Number of turns executed.
        tool_calls_made: Number of tool calls during the run.
        cost_usd: Estimated cost in USD.
    """

    final_text: str
    turns: int = 0
    tool_calls_made: int = 0
    cost_usd: float = 0.0


# ---------------------------------------------------------------------------
# run_stream() — async generator wrapping run()
# ---------------------------------------------------------------------------


async def run_stream(
    *,
    model: Any,
    tools: list[Tool],
    system_prompt: str,
    task: str,
    max_turns: int = 25,
    sandbox: SandboxConfig | None = None,
    allowed_strategies: list[str] | None = None,
    audit_sink: Any | None = None,
    ui_reporter: Any | None = None,
) -> AsyncIterator[StreamEvent]:
    """Run the agent loop and stream events as they occur.

    Wraps arcrun.loop.run() with an event bridge that converts EventBus
    events to typed StreamEvent objects. Token text is streamed word-by-word
    from the final LoopResult content. TurnEndEvent is always the last event.

    Args:
        model: LLM model for the run.
        tools: Tools available to the agent.
        system_prompt: System prompt.
        task: User task.
        max_turns: Maximum turns before halting.
        sandbox: Optional sandbox config.
        allowed_strategies: Optional strategy allowlist.
        audit_sink: Optional arctrust.AuditSink. AuditEvents for stream lifecycle
            (stream.start, stream.end) are emitted to this sink. When None,
            falls back to logger-only (backwards compatible).
        ui_reporter: Optional duck-typed UIEventReporter. When provided,
            emit_run_event() is called for stream lifecycle and tool events.
            No arcui import occurs — caller injects; if None, zero overhead.

    Returns:
        An async iterator of StreamEvent objects.
    """
    # Stable run ID for audit correlation — generated once per stream invocation
    stream_run_id = str(uuid.uuid4())

    # Emit stream.start AuditEvent before starting the loop
    _emit_stream_audit(
        action="stream.start",
        run_id=stream_run_id,
        target=f"stream:{stream_run_id[:8]}",
        outcome="allow",
        extra={"task_prefix": task[:64]},
        sink=audit_sink,
    )

    # Emit stream_start UI event (duck-typed, no arcui import)
    _emit_ui_run_event(
        reporter=ui_reporter,
        event_type="stream_start",
        data={"stream_run_id": stream_run_id, "task_prefix": task[:64]},
    )

    # Queue bridges the synchronous EventBus callbacks into the async iterator
    queue: asyncio.Queue[StreamEvent | None] = asyncio.Queue()

    def _on_event(event: Event) -> None:
        """Bridge EventBus events to stream queue."""
        if event.type == "tool.start":
            name = str(event.data.get("name", ""))
            args = dict(event.data.get("arguments", {}))
            queue.put_nowait(ToolStartEvent(name=name, args=args))
            # Emit tool_start UI event for observability
            _emit_ui_run_event(
                reporter=ui_reporter,
                event_type="tool_start",
                data={"name": name, "args": args},
            )
        elif event.type == "tool.end":
            result = str(event.data.get("result", ""))
            queue.put_nowait(ToolEndEvent(result=result))
            # Emit tool_end UI event for observability
            _emit_ui_run_event(
                reporter=ui_reporter,
                event_type="tool_end",
                data={"result": result},
            )

    # Import here to avoid circular import at module load time
    from arcrun.loop import run

    loop_future: asyncio.Future[LoopResult] = asyncio.get_event_loop().create_future()

    async def _run_loop() -> None:
        try:
            result = await run(
                model,
                tools,
                system_prompt,
                task,
                max_turns=max_turns,
                sandbox=sandbox,
                allowed_strategies=allowed_strategies,
                on_event=_on_event,
            )
            loop_future.set_result(result)
        except Exception as exc:
            loop_future.set_exception(exc)
        finally:
            # Signal the consumer that the run is done
            queue.put_nowait(None)

    return _stream_generator(
        queue,
        loop_future,
        asyncio.ensure_future(_run_loop()),
        stream_run_id=stream_run_id,
        audit_sink=audit_sink,
        ui_reporter=ui_reporter,
    )


async def _stream_generator(
    queue: asyncio.Queue[StreamEvent | None],
    loop_future: asyncio.Future[LoopResult],
    task: asyncio.Task[None],
    *,
    stream_run_id: str,
    audit_sink: Any | None,
    ui_reporter: Any | None,
) -> AsyncIterator[StreamEvent]:
    """Yield StreamEvents from the queue until None sentinel, then TurnEndEvent."""
    while True:
        item = await queue.get()
        if item is None:
            break
        yield item

    # Await the background task to surface any exceptions
    await task

    loop_result = loop_future.result()
    content = loop_result.content or ""

    # Emit token events by splitting content into words to simulate streaming.
    # We emit at least one TokenEvent so the content is always streamed.
    if content:
        words = content.split(" ")
        for i, word in enumerate(words):
            # Re-add the space between words (not before the first word)
            text = (" " + word) if i > 0 else word
            yield TokenEvent(text=text)
            # Mirror each token chunk to the UI reporter for live display
            _emit_ui_run_event(
                reporter=ui_reporter,
                event_type="stream_token",
                data={"text": text, "stream_run_id": stream_run_id},
            )
    else:
        # Empty content: no token events; TurnEndEvent carries final_text=""
        pass

    turn_end = TurnEndEvent(
        final_text=content,
        turns=loop_result.turns,
        tool_calls_made=loop_result.tool_calls_made,
        cost_usd=loop_result.cost_usd,
    )
    yield turn_end

    # Emit stream.end AuditEvent after TurnEndEvent is yielded
    _emit_stream_audit(
        action="stream.end",
        run_id=stream_run_id,
        target=f"stream:{stream_run_id[:8]}",
        outcome="success",
        extra={"turns": loop_result.turns, "tool_calls_made": loop_result.tool_calls_made},
        sink=audit_sink,
    )

    # Emit stream_end UI event after audit so all bookkeeping is complete
    _emit_ui_run_event(
        reporter=ui_reporter,
        event_type="stream_end",
        data={
            "stream_run_id": stream_run_id,
            "turns": loop_result.turns,
            "tool_calls_made": loop_result.tool_calls_made,
            "cost_usd": loop_result.cost_usd,
        },
    )


# ---------------------------------------------------------------------------
# Audit helper — emit AuditEvents via arctrust, fall back to logger
# ---------------------------------------------------------------------------

_logger = logging.getLogger("arcrun.streams")

# Sentinel actor DID used when no per-request identity is available
_STREAM_ACTOR = "did:arc:system:run-stream"


# ---------------------------------------------------------------------------
# UI reporter helper — duck-typed call, no arcui import (layer purity)
# ---------------------------------------------------------------------------


def _emit_ui_run_event(
    *,
    reporter: Any | None,
    event_type: str,
    data: dict[str, Any],
) -> None:
    """Call reporter.emit_run_event() if a reporter is injected.

    Failures are swallowed so UI errors never interrupt stream delivery.
    Duck-typed: no arcui import. Zero overhead when reporter is None.
    """
    if reporter is None:
        return
    try:
        reporter.emit_run_event(event_type=event_type, data=data)
    except Exception:
        _logger.warning(
            "UIReporter.emit_run_event failed event_type=%s — swallowing",
            event_type,
            exc_info=True,
        )


def _emit_stream_audit(
    *,
    action: str,
    run_id: str,
    target: str,
    outcome: str,
    extra: dict[str, Any] | None = None,
    sink: Any | None,
) -> None:
    """Emit an AuditEvent for a stream lifecycle event.

    Falls back to logger-only when arctrust is unavailable or sink is None.
    Per NIST AU-5, sink failures are swallowed so auditing never breaks streaming.
    """
    if sink is None:
        return
    try:
        from arctrust import AuditEvent, emit

        event = AuditEvent(
            actor_did=_STREAM_ACTOR,
            action=action,
            target=target,
            outcome=outcome,
            request_id=run_id,
            extra={
                "stream_run_id": run_id,
                **(extra or {}),
            },
        )
        emit(event, sink)
    except Exception:
        _logger.warning(
            "Failed to emit AuditEvent action=%s run_id=%s — swallowing (AU-5)",
            action,
            run_id,
            exc_info=True,
        )
