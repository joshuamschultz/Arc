"""Streaming runtime API for arcrun.

Two entry points:

- ``run_stream(...)`` wraps the full ReAct loop and yields ``StreamEvent``
  objects (token, tool start/end, turn end). Token text is derived from
  the final ``LoopResult.content`` via word splitting — convenient when
  the underlying model adapter doesn't expose a real streaming wire.

- ``stream_llm_response(model, messages, ...)`` streams a single
  ``model.invoke_stream`` call as ``TokenEvent`` then ``TurnEndEvent``.
  When the wrapped adapter implements real streaming (OpenAI and the
  family inheriting from it) the deltas land as they arrive — this is
  the primitive demos use for the chat-typing-effect UX without
  carrying loop overhead. Adapters that only ship the default
  ``invoke_stream`` fallback still work; you just see one big
  ``TokenEvent`` per call.

This module is pure arcrun — no LLM calls, no agent state.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from arcrun.capabilities import CapabilityProvider
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


@dataclass
class RunResult:
    """Final result of a streamed run, reconstructed by ``collect()``.

    The streaming entry (``agent.run``) is the single way to drive an agent;
    one-shot callers (CLI, scheduler, module callbacks) that only want the
    final answer drain the stream through ``collect()`` to get this. Carries
    exactly what the terminal ``TurnEndEvent`` reports — no loop internals
    (``tokens_used``/``strategy_used`` stay inside ``LoopResult``).
    """

    content: str
    turns: int = 0
    tool_calls_made: int = 0
    cost_usd: float = 0.0


async def collect(stream: AsyncIterator[StreamEvent]) -> RunResult:
    """Drain a StreamEvent iterator to a final RunResult.

    Consumes the whole stream. The terminal ``TurnEndEvent`` carries the
    authoritative totals; if a stream ends without one, the concatenated
    ``TokenEvent`` text is used as the content fallback so callers always
    get the response text.
    """
    token_text: list[str] = []
    turn_end: TurnEndEvent | None = None
    async for event in stream:
        if isinstance(event, TurnEndEvent):
            turn_end = event
        elif isinstance(event, TokenEvent):
            token_text.append(event.text)
    if turn_end is not None:
        return RunResult(
            content=turn_end.final_text,
            turns=turn_end.turns,
            tool_calls_made=turn_end.tool_calls_made,
            cost_usd=turn_end.cost_usd,
        )
    return RunResult(content="".join(token_text))


# ---------------------------------------------------------------------------
# run_stream() — async generator wrapping run()
# ---------------------------------------------------------------------------


async def run_stream(
    *,
    model: Any,
    capabilities: CapabilityProvider,
    system_prompt: str,
    task: str,
    messages: list[Any] | None = None,
    max_turns: int = 25,
    sandbox: SandboxConfig | None = None,
    allowed_strategies: list[str] | None = None,
    on_event: Callable[[Event], None] | None = None,
    transform_context: Callable[..., Any] | None = None,
    tool_choice: dict[str, Any] | None = None,
    actor_did: str | None = None,
    store_raw_bodies: bool = False,
    audit_sink: Any | None = None,
    ui_reporter: Any | None = None,
) -> AsyncIterator[StreamEvent]:
    """Run the agent loop and stream events as they occur.

    Wraps arcrun.loop.run() with an event bridge that converts EventBus
    events to typed StreamEvent objects. Token text is streamed word-by-word
    from the final LoopResult content. TurnEndEvent is always the last event.

    Args:
        model: LLM model for the run.
        capabilities: CapabilityProvider whose advertised specs become the
            model's tool list; calls route to ``provider.invoke``.
        system_prompt: System prompt.
        task: User task.
        messages: Prior session history to seed the loop (history parity with
            the blocking path). When None, a fresh single-turn run from ``task``.
        max_turns: Maximum turns before halting.
        sandbox: Optional sandbox config.
        allowed_strategies: Optional strategy allowlist.
        on_event: Optional external EventBus bridge. Loop events are delivered
            to it in addition to the internal stream bridge — this is the seam
            SPEC-026 recording and module telemetry ride on, so the streaming
            entry records exactly like the blocking one did.
        transform_context: Optional context transformer forwarded to the loop.
        tool_choice: Optional forced tool-choice forwarded to the loop.
        actor_did: Optional caller DID forwarded to the loop's EventBus (spool).
        store_raw_bodies: When True, tool argument + result bodies ride the spool
            (so the observability surface can show tool in/out); off by default.
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
        """Bridge EventBus events to stream queue.

        Also forwards every event to the caller-supplied ``on_event`` so the
        recording/telemetry bridge sees the same events as the blocking path.
        """
        if on_event is not None:
            on_event(event)
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
                capabilities,
                system_prompt,
                task,
                messages=messages,
                max_turns=max_turns,
                sandbox=sandbox,
                allowed_strategies=allowed_strategies,
                on_event=_on_event,
                transform_context=transform_context,
                tool_choice=tool_choice,
                actor_did=actor_did,
                store_raw_bodies=store_raw_bodies,
            )
            loop_future.set_result(result)
        except Exception as exc:  # reason: fail-open — continue
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


_logger = logging.getLogger("arcrun.streams")


# ---------------------------------------------------------------------------
# stream_llm_response() — single-call streaming primitive
# ---------------------------------------------------------------------------


async def stream_llm_response(
    *,
    model: Any,
    messages: list[Any],
    tools: list[Tool] | None = None,
    **invoke_kwargs: Any,
) -> AsyncIterator[StreamEvent]:
    """Stream one ``model.invoke_stream`` call as StreamEvents.

    Yields one or more ``TokenEvent`` followed by exactly one
    ``TurnEndEvent``. No loop, no tool execution — this is the
    typing-effect primitive for demos that want to show a single
    LLM response landing piece by piece. Tool calls in the stream
    are ignored (the response is treated as text-only); callers that
    need tool dispatch should use ``run_stream`` instead.

    When the wrapped adapter overrides ``invoke_stream`` (OpenAI and
    family), deltas arrive at wire speed. When it doesn't, the
    default fallback wraps ``invoke()`` and yields the full content
    as one big delta — still correct, just not progressive.
    """
    content_parts: list[str] = []
    final_usage: Any = None
    final_stop_reason: Any = None

    async for delta in model.invoke_stream(messages, tools=tools, **invoke_kwargs):
        if delta.text:
            content_parts.append(delta.text)
            yield TokenEvent(text=delta.text)
        if delta.usage is not None:
            final_usage = delta.usage
        if delta.stop_reason is not None:
            final_stop_reason = delta.stop_reason

    final_text = "".join(content_parts)
    yield TurnEndEvent(
        final_text=final_text,
        turns=1,
        tool_calls_made=0,
        cost_usd=0.0,
    )

    _logger.debug(
        "stream_llm_response complete len=%d usage=%s stop_reason=%s",
        len(final_text),
        final_usage,
        final_stop_reason,
    )


# ---------------------------------------------------------------------------
# Audit helper — emit AuditEvents via arctrust, fall back to logger
# ---------------------------------------------------------------------------

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
    except Exception:  # reason: fail-open — log + continue
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
    except Exception:  # reason: fail-open — log + continue
        _logger.warning(
            "Failed to emit AuditEvent action=%s run_id=%s — swallowing (AU-5)",
            action,
            run_id,
            exc_info=True,
        )
