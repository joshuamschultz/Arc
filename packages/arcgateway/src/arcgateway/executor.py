"""Executor Protocol and implementations for running ArcAgents.

Design (SDD §3.1 Process Model):

    Executor Protocol — the contract all executors must satisfy.
    AsyncioExecutor  — personal/enterprise: runs ArcAgent in-process via asyncio.
    SubprocessExecutor — federal-tier: spawns arc-agent-worker subprocess (T1.6).

``NATSExecutor`` (multi-instance scaling, deferred with no ETA) lives in
``arcgateway.executor_nats`` — split out to keep this module inside the
arcgateway core LOC budget (ADR-004 / G1.6). Import it from there.

The executor is chosen by the tier-policy layer in GatewayRunner. Callers
only see the Executor Protocol; tier logic is not scattered through business code.

Module boundary: arcgateway.executor MAY reach the agent through its public
facade to deliver a message. arcagent MUST NOT import anything from arcgateway.

Implementation contract for run():
    run() is an async coroutine that returns an AsyncIterator[Delta].
    It is NOT an async generator itself. The separation keeps run() callable
    as a regular coroutine (``delta_iter = await executor.run(event)``) while
    the actual streaming happens in the returned iterator. This allows callers
    to detect connection/auth failures from run() without starting to consume
    deltas, and allows the executor to set up context before returning.

SubprocessExecutor / ResourceLimits / _make_preexec_fn live in
executor_subprocess.py (extracted per ADR-004 / G1.6 LOC budget). They are
re-exported from this module so that existing imports
``from arcgateway.executor import SubprocessExecutor, ResourceLimits``
continue to work unchanged.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field, model_validator

from arcgateway.parts import Part, TextPart, flatten_text

_logger = logging.getLogger("arcgateway.executor")


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class InboundEvent(BaseModel):
    """Normalised inbound message from any platform adapter.

    All platform-specific details have been resolved before this point:
    - user_did is the resolved cross-platform user identity (D-06).
    - agent_did identifies which ArcAgent should handle this message.
    - session_key is stamped by SessionRouter, not by the adapter.

    Attributes:
        platform: Source platform name ("telegram", "slack", etc.).
        chat_id: Platform-specific conversation identifier.
        thread_id: Optional thread within the chat.
        user_did: Resolved user DID (cross-platform identity).
        agent_did: Target agent DID.
        session_key: Canonical session key, empty until SessionRouter stamps it.
            Adapters supply platform identity only and leave this unset: the
            router is the sole owner of session identity (REQ-304, REQ-310),
            and a key composed anywhere else is a second identity for the same
            (agent, user) pair that also skips the rotation generation.
        message: The message as text — the flattened projection of ``parts``
            that every text-only consumer (commands, the subprocess executor,
            the echo stub) reads.
        parts: The message as the sender composed it: an ordered list in which
            text is a part like any other and an artefact is a *reference* into
            the agent workspace, never bytes (SPEC-065 REQ-296). Media therefore
            never becomes a branch, and a 5MB photo never enters the queue, the
            session jsonl or the prompt.
        raw_payload: Full platform-specific payload for audit/replay.
    """

    platform: str
    chat_id: str
    thread_id: str | None = None
    user_did: str
    agent_did: str
    session_key: str = ""
    message: str
    parts: list[Part] = Field(default_factory=list)
    raw_payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _keep_projections_consistent(self) -> InboundEvent:
        """Make ``message`` and ``parts`` two views of one message, not two messages.

        A caller supplies whichever is natural — text for the many surfaces that
        only ever have text, parts for an adapter that took a photo off the
        wire — and reads either. Letting them diverge is how a media message
        ends up delivered as its caption alone.

        ``parts`` wins whenever it is present, because it is the richer view:
        it can express an artefact, and ``message`` cannot. Reconciling only
        when one side was missing left the case that actually bites — a caller
        supplying BOTH, inconsistently — to survive validation with two
        contradictory views, which is the exact divergence this guards against.

        When both were supplied and disagreed, each view held something the
        other had dropped: text present in ``message`` but in no part is
        discarded here, and that loss is logged rather than made silent. It is
        a caller bug — every in-tree producer builds ``parts`` first — and a
        vanishing caption is far harder to trace from the symptom than from a
        warning naming the event. Refusing the message outright would lose the
        whole turn instead of one field, which is the worse trade on an inbound
        path.
        """
        if not self.parts:
            if self.message:
                self.parts = [TextPart(text=self.message)]
            return self

        projection = flatten_text(self.parts)
        if self.message and self.message != projection:
            _logger.warning(
                "InboundEvent on %s carried a message its parts do not express; "
                "parts win and the divergent text is dropped. Build parts first "
                "and let message be their projection. dropped=%r kept=%r",
                self.platform,
                self.message,
                projection,
            )
        self.message = projection
        return self


class Delta(BaseModel):
    """One streamed chunk from an executor run.

    Adapts the streaming contract from ArcRun event bus into a simple
    flat structure that StreamBridge can forward to the platform adapter.

    Attributes:
        kind: "token" for LLM output text, "tool_call" for tool invocations,
            "done" for the final sentinel with full summary.
        content: Text fragment (for kind=="token") or tool call description
            (for kind=="tool_call"). Empty string for "done".
        is_final: True only on the terminal "done" delta.
        turn_id: Run-level turn identifier for idempotency keys.
    """

    kind: Literal["token", "tool_call", "done"]
    content: str = ""
    is_final: bool = False
    turn_id: str = ""


# ---------------------------------------------------------------------------
# Executor Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Executor(Protocol):
    """Contract for running an ArcAgent in response to an InboundEvent.

    run() is a coroutine that returns an AsyncIterator[Delta]. It is NOT
    an async generator function. This separation means:
    - Callers can ``delta_iter = await executor.run(event)`` to set up context.
    - Streaming happens when the caller does ``async for delta in delta_iter``.
    - Failures before streaming begins (auth, connection) raise from run().

    All implementations must:
    - Be safe to call concurrently (multiple sessions in parallel).
    - Never share mutable state across concurrent run() calls.
    - Always yield a final Delta(kind="done", is_final=True) as the last item.
    - Emit structured logs (never print statements).
    """

    async def run(self, event: InboundEvent) -> AsyncIterator[Delta]:
        """Execute agent run for the given inbound event.

        Args:
            event: Normalised platform-agnostic inbound message.

        Returns:
            AsyncIterator[Delta] to consume streamed output chunks.

        Raises:
            RuntimeError: On unrecoverable executor failure before streaming begins.
        """
        ...  # Protocol body — not called


def _reply_target(event: InboundEvent) -> str:
    """The channel address a turn arrived on, as ``platform:chat_id[:thread_id]``.

    Handed to the agent's delivery entry point so a schedule created
    mid-conversation defaults its delivery back to this channel (arcagent stays
    string-only; it never parses this). Mirrors ``DeliveryTarget``'s canonical
    form without importing it.
    """
    base = f"{event.platform}:{event.chat_id}"
    return f"{base}:{event.thread_id}" if event.thread_id else base


def _reply_label(event: InboundEvent) -> str:
    """A human-friendly name for this channel, for arcui's delivery dropdown.

    Prefers the sender's name from the platform payload (Telegram first_name /
    username, a chat title) so a non-technical operator sees "Telegram — Josh"
    instead of a raw chat id; falls back to the platform + chat id.
    """
    raw = event.raw_payload or {}
    name = raw.get("first_name") or raw.get("username") or raw.get("title")
    platform = event.platform.capitalize()
    return f"{platform} — {name}" if name else f"{platform} (chat {event.chat_id})"


# ---------------------------------------------------------------------------
# AsyncioExecutor — personal / enterprise tier
# ---------------------------------------------------------------------------

# Type alias for the agent factory callable.
# Signature: async (agent_did: str) -> agent. The agent must expose
# ``deliver_message(*, caller_did, message, session_key, reply_target,
# reply_label, on_handle) -> str`` (ArcAgent satisfies it).
AgentFactory = Callable[[str], Any]


class AsyncioExecutor:
    """In-process executor using asyncio tasks.

    Suitable for personal and enterprise tiers where process isolation
    is not a federal compliance requirement. Runs ArcAgent directly in
    the gateway's event loop.

    Agent integration:
        Accepts an optional ``agent_factory`` async callable with signature
        ``async (agent_did: str) -> agent``.  The returned object must expose
        ``deliver_message(...)`` (ArcAgent satisfies it). The executor hands the
        message over and adapts the reply of any turn that delivery opened into
        a token ``Delta``. It makes no decision about the run itself.

        When ``agent_factory`` is None the executor falls back to the
        echo stub so tests can exercise routing/session mechanics without
        an installed ArcAgent configuration. No production code path
        constructs an ``AsyncioExecutor`` with no ``agent_factory``:
        ``bootstrap.build_for_embedded`` (the embedded gateway, canonical
        at every tier) always supplies a real one, and the standalone
        ``arc gateway start`` CLI (``cli.cmd_start``) refuses to start at
        all rather than reach this fallback. Reaching the echo stub means
        either a test, or code that bypassed both of those and constructed
        this class directly.

        The factory is called once per event — the caller is responsible
        for caching agents if startup cost is significant.

    Attributes:
        _agent_factory: Optional async callable producing an agent instance.
    """

    def __init__(self, agent_factory: AgentFactory | None = None) -> None:
        """Initialise AsyncioExecutor.

        Args:
            agent_factory: Optional async callable ``(agent_did: str) -> agent``.
                When provided, the executor hands each message to
                ``agent.deliver_message(...)`` and adapts the reply of a turn
                that opened into a Delta. When None, the echo stub is used (for
                tests and dev without a real ArcAgent config).
        """
        self._agent_factory = agent_factory

    def set_agent_factory(self, agent_factory: AgentFactory | None) -> None:
        """Replace the agent factory after construction.

        Used by arcui's ``embedded_agents.install_embedded_agent_hooks``
        to wrap the bootstrap-built factory with a cache + fleet-registry
        hook without losing the original load logic. Pass ``None`` to
        revert to the echo stub (useful in tests).
        """
        self._agent_factory = agent_factory

    @property
    def agent_factory(self) -> AgentFactory | None:
        """Expose the current agent factory for wrapping.

        Wrappers should pull the current factory, build their wrapper
        closure around it, then call ``set_agent_factory(wrapped)`` —
        atomic replace, no private-attribute mutation required.
        """
        return self._agent_factory

    async def run(self, event: InboundEvent) -> AsyncIterator[Delta]:
        """Run ArcAgent in-process for the given event.

        Returns an async iterator of Delta chunks.  If an ``agent_factory``
        was provided the message is handed to the real ArcAgent's delivery
        entry point; otherwise the echo stub is used.

        Args:
            event: Normalised inbound event.

        Returns:
            AsyncIterator[Delta] yielding agent output.
        """
        _logger.debug(
            "AsyncioExecutor.run: platform=%s session=%s agent_factory=%s",
            event.platform,
            event.session_key,
            "wired" if self._agent_factory is not None else "stub",
        )
        return self._stream(event)

    async def _stream(self, event: InboundEvent) -> AsyncIterator[Delta]:
        """Internal async generator; separated so run() stays a regular coroutine.

        When ``_agent_factory`` is set:
          1. ``await _agent_factory(event.agent_did)`` obtains the agent.
          2. ``await agent.deliver_message(...)`` hands the message over. The
             AGENT decides what happens to it — join the turn in flight, or open
             a new one — and the gateway states no preference: no interrupt flag
             crosses this seam (REQ-302, REQ-303, REQ-312). Opening a turn
             opens-or-resumes the agent's session for this channel, so every turn
             still appends to ``<workspace>/sessions/<session_key>.jsonl``, which
             is what surfaces in arcui and gives reconnect history.
          3. When the message opened a turn, ``on_handle`` hands back that run and
             its final content becomes the reply this channel streams. When the
             message joined a run already in flight there is nothing to stream
             here: its answer is part of that run's reply, delivered on the turn
             it joined — one reply, not two.

        An error fails closed: a single fail-closed token Delta is emitted, the
        done sentinel closes the turn, and no partial-success claim is made
        (AC-3.2).

        When ``_agent_factory`` is None the echo stub is used instead so that
        all existing tests continue to pass without a real agent configured.

        Args:
            event: Inbound event to process.
        """
        if self._agent_factory is not None:
            turn_id = str(uuid.uuid4())
            # Typed Any: the run handle is an arcrun type, and arcgateway must
            # not import a model/runtime package to name it (architecture guard).
            opened: list[Any] = []
            try:
                agent = await self._agent_factory(event.agent_did)
                # ``parts`` only when the message is more than its words. The
                # agent then translates the references at its own boundary
                # (PartTranslator, COMP-009); every text-only surface takes the
                # path it always did, unchanged.
                extra: dict[str, Any] = {}
                if any(part.kind != "text" for part in event.parts):
                    extra["parts"] = [part.model_dump() for part in event.parts]
                outcome = await agent.deliver_message(
                    caller_did=event.user_did,
                    message=event.message,
                    session_key=event.session_key,
                    reply_target=_reply_target(event),
                    reply_label=_reply_label(event),
                    on_handle=opened.append,
                    **extra,
                )
                _logger.debug(
                    "AsyncioExecutor: delivery session=%s outcome=%s",
                    event.session_key,
                    outcome,
                )
                for handle in opened:
                    result = await handle.result()
                    content = result.content or ""
                    if content:
                        yield Delta(
                            kind="token",
                            content=content,
                            is_final=False,
                            turn_id=turn_id,
                        )
            except Exception as exc:  # reason: fail-closed — log + close turn
                _logger.exception(
                    "AsyncioExecutor: agent error session=%s: %s",
                    event.session_key,
                    exc,
                )
                # Don't leak raw exception text (paths, URLs, secrets) to the
                # channel — the detail is in the log above (LLM02/LLM07).
                yield Delta(
                    kind="token",
                    content="[agent-error] the run failed; see server logs",
                    is_final=False,
                    turn_id=turn_id,
                )
            yield Delta(kind="done", content="", is_final=True, turn_id=turn_id)
            return

        # --- echo stub (no agent_factory configured) ---
        yield Delta(
            kind="token",
            content=(
                f"[AsyncioExecutor stub] Received: {event.message!r} (session={event.session_key})"
            ),
            is_final=False,
            turn_id=event.session_key,
        )
        yield Delta(kind="done", content="", is_final=True, turn_id=event.session_key)


# ---------------------------------------------------------------------------
# SubprocessExecutor / ResourceLimits / _make_preexec_fn — re-exported
# ---------------------------------------------------------------------------
# These live in executor_subprocess.py (ADR-004 / G1.6 LOC budget).
# Re-exported here so existing imports continue to work unchanged:
#   from arcgateway.executor import SubprocessExecutor, ResourceLimits

from arcgateway.executor_subprocess import (  # noqa: E402 — intentional late import
    ResourceLimits,
    SubprocessExecutor,
    _make_preexec_fn,
)

__all__ = [
    "AgentFactory",
    "AsyncioExecutor",
    "Delta",
    "Executor",
    "InboundEvent",
    "ResourceLimits",
    "SubprocessExecutor",
    "_make_preexec_fn",
]
