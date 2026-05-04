"""Executor Protocol and implementations for running ArcAgents.

Design (SDD §3.1 Process Model):

    Executor Protocol — the contract all executors must satisfy.
    AsyncioExecutor  — personal/enterprise: runs ArcAgent in-process via asyncio.
    SubprocessExecutor — federal-tier: spawns arc-agent-worker subprocess (T1.6).
    NATSExecutor     — multi-instance scaling (deferred, no ETA).

The executor is chosen by the tier-policy layer in GatewayRunner. Callers
only see the Executor Protocol; tier logic is not scattered through business code.

Module boundary: arcgateway.executor MAY import arcagent to call agent.run().
arcagent MUST NOT import anything from arcgateway.

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

from pydantic import BaseModel, Field

_logger = logging.getLogger("arcgateway.executor")


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class InboundEvent(BaseModel):
    """Normalised inbound message from any platform adapter.

    All platform-specific details have been resolved before this point:
    - user_did is the resolved cross-platform user identity (D-06).
    - agent_did identifies which ArcAgent should handle this message.
    - session_key is pre-computed by SessionRouter.

    Attributes:
        platform: Source platform name ("telegram", "slack", etc.).
        chat_id: Platform-specific conversation identifier.
        thread_id: Optional thread within the chat.
        user_did: Resolved user DID (cross-platform identity).
        agent_did: Target agent DID.
        session_key: Pre-computed session key (build_session_key output).
        message: Raw text content from the user.
        raw_payload: Full platform-specific payload for audit/replay.
    """

    platform: str
    chat_id: str
    thread_id: str | None = None
    user_did: str
    agent_did: str
    session_key: str
    message: str
    raw_payload: dict[str, Any] = Field(default_factory=dict)


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


# ---------------------------------------------------------------------------
# AsyncioExecutor — personal / enterprise tier
# ---------------------------------------------------------------------------

# Type alias for the agent factory callable.
# Signature: async (agent_did: str) -> agent_object_with_run_method
AgentFactory = Callable[[str], Any]


class AsyncioExecutor:
    """In-process executor using asyncio tasks.

    Suitable for personal and enterprise tiers where process isolation
    is not a federal compliance requirement. Runs ArcAgent directly in
    the gateway's event loop.

    Agent integration (M1 final integration):
        Accepts an optional ``agent_factory`` async callable with signature
        ``async (agent_did: str) -> agent``.  The returned object must
        have ``async run(prompt: str) -> Any`` (ArcAgent.run satisfies this).

        When ``agent_factory`` is None the executor falls back to the
        echo stub so all existing tests continue to pass without an
        installed ArcAgent configuration.

        The factory is called once per event — the caller is responsible
        for caching agents if startup cost is significant.

    Attributes:
        _agent_factory: Optional async callable producing an agent instance.
    """

    def __init__(self, agent_factory: AgentFactory | None = None) -> None:
        """Initialise AsyncioExecutor.

        Args:
            agent_factory: Optional async callable ``(agent_did: str) -> agent``.
                When provided, the executor calls ``await agent.run(event.message)``
                and wraps the result in Delta objects.
                When None, the echo stub is used (for tests and dev without
                a real ArcAgent config).
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
        was provided the real ArcAgent is invoked; otherwise the echo stub
        is used.

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
          1. Calls ``await _agent_factory(event.agent_did)`` to obtain an agent.
          2. Calls ``await agent.chat(event.message, session_id=event.session_key)``
             so every turn is appended to the agent's persistent session log
             at ``<workspace>/sessions/<session_key>.jsonl``. This is what
             surfaces in arcui's Sessions tab and provides chat history on
             reconnect (Slack/Telegram/Web all share the same path).
          3. Extracts text from ``result.content`` (ArcRun result) or str(result).
          4. Yields one token Delta with the full response, then the done sentinel.

        ArcAgent does not expose a true streaming iterator today — it returns
        a complete result from chat().  The single-token wrapping is honest:
        the whole response arrives as one chunk.  Streaming will be possible
        once ArcRun exposes an async event stream (tracked as M2 work).

        When ``_agent_factory`` is None the echo stub is used instead so that
        all existing tests continue to pass without a real agent configured.

        Args:
            event: Inbound event to process.
        """
        if self._agent_factory is not None:
            turn_id = str(uuid.uuid4())
            try:
                agent = await self._agent_factory(event.agent_did)
                # Prefer ``agent.chat(message, session_id=...)`` — appends the
                # turn to the agent's persistent SessionManager log so the
                # arcui Sessions tab and Messages page reconnect history
                # work out of the box. Fall back to ``agent.run(message)``
                # for stateless agent factories (test fakes, simple
                # one-shot agents) that don't expose ``chat``.
                # session_key is deterministic per (agent_did, user_did) — same
                # browser tab, slack DM, or telegram chat resumes the same
                # session across reconnects.
                if hasattr(agent, "chat"):
                    result = await agent.chat(
                        event.message, session_id=event.session_key
                    )
                else:
                    result = await agent.run(event.message)
                # ArcRun returns a result object; .content holds the text reply.
                content: str = getattr(result, "content", None) or str(result)
                yield Delta(
                    kind="token",
                    content=content,
                    is_final=False,
                    turn_id=turn_id,
                )
            except Exception as exc:
                _logger.exception(
                    "AsyncioExecutor: agent error session=%s: %s",
                    event.session_key,
                    exc,
                )
                yield Delta(
                    kind="token",
                    content=f"[agent-error] {exc}",
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


# ---------------------------------------------------------------------------
# NATSExecutor — multi-instance scaling (deferred)
# ---------------------------------------------------------------------------


class NATSExecutor:
    """NATS-backed executor for multi-instance gateway deployments.

    Routes agent execution to worker nodes via NATS subject addressing.
    Required when a single bot token serves multiple gateway replicas behind
    a load balancer.

    Implementation deferred — no ETA. See SDD §6 open question on
    NATS-vs-in-process queue for >1 instance (SPEC-018).
    """

    async def run(self, event: InboundEvent) -> AsyncIterator[Delta]:
        """Dispatch event to NATS worker and stream response.

        Raises:
            NotImplementedError: Multi-instance scaling is deferred.
        """
        raise NotImplementedError(
            "NATSExecutor: multi-instance NATS-based scaling is deferred. "
            "No implementation ETA in SPEC-018. See SDD §6."
        )
