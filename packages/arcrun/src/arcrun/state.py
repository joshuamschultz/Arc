"""Internal mutable state for a single run() execution."""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import arcllm

from arcrun._messages import content_text, user_message
from arcrun.checkpoint import LoopCheckpoint
from arcrun.dynamic.seal import RunSeal
from arcrun.events import EventBus
from arcrun.ledger import ToolExecutionLedger
from arcrun.registry import ToolRegistry

#: How much of a held message's text rides its ``message.injected`` audit event.
_HELD_PREVIEW_LEN = 120


class RunDeadlineExceededError(TimeoutError):
    """Raised when a run reaches its absolute monotonic deadline."""


class RunWorkCancelledError(Exception):
    """Raised inside the loop when operator cancellation interrupts active work."""


@dataclass(frozen=True)
class Injection:
    """A steer/follow_up message tagged with its verified caller identity.

    arcrun is a dumb but *identified* queue: it never decides whether an
    injection is permitted — that trust/policy decision belongs to the caller
    (the host). arcrun only guarantees the injection carries a non-empty
    ``caller_did`` and records it in the audit trail at the drain point.
    """

    caller_did: str
    message: str | list[arcllm.ContentBlock]
    message_id: str

    @property
    def preview_text(self) -> str:
        """The injection's words, for the audit event's preview field.

        A block message has no single string, and the audit line must still say
        what arrived rather than a repr of the list.
        """
        return content_text(self.message)

    @classmethod
    def new(cls, caller_did: str, message: str | list[arcllm.ContentBlock]) -> Injection:
        """Build an injection, requiring a non-empty ``caller_did``.

        The ``message_id`` is minted here so the enqueue and the later drain-time
        audit event refer to the same identifier.
        """
        if not caller_did:
            raise ValueError("caller_did is required to inject a steering message")
        return cls(caller_did=caller_did, message=message, message_id=str(uuid.uuid4()))


@dataclass
class RunState:
    """Internal state during execution. Not part of public API."""

    messages: list[Any]
    registry: ToolRegistry
    event_bus: EventBus
    turn_count: int = 0
    tokens_used: dict[str, int] = field(
        default_factory=lambda: {"input": 0, "output": 0, "total": 0}
    )
    cost_usd: float = 0.0
    tool_calls_made: int = 0
    run_id: str = ""
    # Durable home for anything a run needs to outlive the process — today the
    # dynamic strategy's replay journal and script scratch files. arcrun never
    # invents this path: only the caller knows where an agent is allowed to
    # write (ADR-029), so ``None`` simply means nothing is persisted and a
    # paused script cannot be resumed after a restart.
    work_dir: Path | None = None
    # Operator custody over the files a run resumes from. The host injects it
    # because arcrun holds no key material and resolves no Arc-home path;
    # ``None`` signs and verifies nothing, so every tier runs one code path.
    seal: RunSeal | None = None
    depth: int = 0
    max_depth: int = 3
    parent_run_id: str = ""
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    # Operator attribution for a hard cancel (kill switch, ASI09/ASI10). Set by
    # ``RunHandle.cancel`` before the event so the cancel terminator can name the
    # caller (and optional reason) in the structured result and audit event.
    # arcrun records but does not authorize — that policy call is the caller's.
    cancelled_by: str = ""
    cancel_reason: str = ""
    deadline: float | None = None
    active_work: set[asyncio.Future[Any]] = field(default_factory=set)
    tool_ledger: ToolExecutionLedger | None = None
    steer_queue: asyncio.Queue[Injection] = field(default_factory=lambda: asyncio.Queue(maxsize=16))
    followup_queue: asyncio.Queue[Injection] = field(
        default_factory=lambda: asyncio.Queue(maxsize=16)
    )
    # Caller hook applied to the message list before each model call.
    # CONTRACT: append-only between turns — the returned list must keep the
    # input's prefix so the provider cache prefix stays valid; only the tail
    # may grow. A deliberate compaction may return a *shorter* list (a one-time
    # boundary reset), but must never rewrite/reorder earlier messages per turn.
    # Set ARCRUN_ASSERT_APPEND_ONLY=1 to enforce this in dev.
    transform_context: Callable[..., Any] | None = None
    tool_timeout: float | None = None
    strategy_name: str = ""
    tool_choice: dict[str, Any] | None = None
    # SPEC-017 R-030/R-032 — structured task completion.
    # When ``task_complete`` fires, the tool handler stores its payload
    # here so the strategy can terminate cleanly on the next check.
    # ``None`` means no termination requested.
    completion_payload: dict[str, Any] | None = None
    # Name of the tool whose ``signals_completion=True`` flag ended the
    # loop. Surfaces on LoopResult so callers can distinguish multiple
    # terminator tools without re-scanning the event chain.
    completion_tool: str | None = None
    # Hard caps from config; enforced at the top of each turn. Token is the
    # primary ceiling (present on both streaming and non-streaming paths);
    # cost is the best-effort secondary (non-streaming, priced models only).
    max_cost_usd: float | None = None
    max_tokens: int | None = None
    # SPEC-043 — turn cap, mirrored onto state so the unified breaker
    # (check_breaker) and the checkpoint emitter can read it without threading
    # the loop parameter through every call site. Set by the strategy at start.
    max_turns: int = 0
    # SPEC-043 REQ-001/002 — turn-boundary checkpoint hook. When set, the loop
    # calls it at each turn boundary with a serializable LoopCheckpoint. arcrun
    # never persists; the caller does. None ⇒ zero hot-path overhead.
    on_checkpoint: Callable[[LoopCheckpoint], None] | None = None
    # SPEC-043 REQ-010..012 — proactive HITL pause. Before dispatching a call to
    # a tool named in ``approval_required_tools``, the loop awaits
    # ``approval_provider(tc)``; a returned grant is attached to the call, ``None``
    # fails closed (call not dispatched). arcrun mints/verifies nothing — the
    # provider is bound to the host's human-approval gate.
    approval_provider: Callable[[Any], Awaitable[Any]] | None = None
    approval_required_tools: frozenset[str] = frozenset()
    # SPEC-043 REQ-035 — semaphore ceiling on concurrent in-flight tool calls.
    max_parallel: int = 10
    # SPEC-043 REQ-020/021/024 — unified circuit-breaker thresholds. ``None``
    # disables a breaker (personal may relax; federal supplies non-relaxable
    # floors). ``max_repeat``: identical tool-call signatures before a runaway
    # trip. ``max_consecutive_errors``: consecutive tool failures before a
    # cascade trip.
    max_repeat: int | None = None
    max_consecutive_errors: int | None = None
    # Breaker running state (REQ-020/021/025). ``runaway_signature`` is the last
    # single-call signature seen; ``runaway_count`` its consecutive-turn streak
    # (a distinct-signature batch resets it — that is progress, REQ-025).
    runaway_signature: str | None = None
    runaway_count: int = 0
    consecutive_tool_errors: int = 0
    stream_event: Callable[[str, dict[str, Any]], None] | None = None
    # H-038 — per-call current-time context. The host injects a clock exactly
    # like ``actor_did``/``run_origin`` (a caller-supplied callable, never a
    # config read): ``None`` uses arcrun's zero-config default (real UTC,
    # resolved in ``strategies.react``). A host with its own timezone or a
    # test/replay path that needs to pin the exact recorded value overrides it
    # here — arcrun never reaches up for either.
    clock: Callable[[], datetime] | None = None

    def remaining_seconds(self) -> float | None:
        """Return remaining run budget or raise on expiry."""
        if self.deadline is None:
            return None
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RunDeadlineExceededError("run deadline exceeded")
        return remaining

    async def await_work(self, awaitable: Awaitable[Any]) -> Any:
        """Await cancellable work inside the one absolute run deadline."""
        work = asyncio.ensure_future(awaitable)
        cancelled = asyncio.create_task(self.cancel_event.wait())
        self.active_work.add(work)
        try:
            done, _pending = await asyncio.wait(
                {work, cancelled},
                timeout=self.remaining_seconds(),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if work in done:
                if self.cancel_event.is_set() and work.cancelled():
                    raise RunWorkCancelledError("run work cancelled")
                return work.result()
            work.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await work
            if cancelled in done:
                raise RunWorkCancelledError("run work cancelled")
            raise RunDeadlineExceededError("run deadline exceeded")
        finally:
            if not work.done():
                work.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await work
            cancelled.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cancelled
            self.active_work.discard(work)

    async def cancel_active_work(self) -> None:
        """Cancel and await all active child operations."""
        active = tuple(self.active_work)
        for work in active:
            work.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)

    # -- steering: held messages, entered at turn boundaries --------------------
    # The whole of steering lives here, on the state every strategy already holds,
    # so a strategy's loop calls ONE method at its turn boundary instead of each
    # re-implementing queue draining (DRY). A ``steer`` and a ``follow_up`` are the
    # same simple thing: a message that arrived while a turn ran, held on a queue,
    # then entered into context at the next turn boundary — never mid-tool, never
    # one-at-a-time, never dropped.

    def has_held_messages(self) -> bool:
        """True when a message arrived and is waiting to be entered next turn."""
        return not self.steer_queue.empty() or not self.followup_queue.empty()

    def enter_held_messages(self) -> None:
        """Enter EVERY held message into context as a user turn, oldest first.

        Called by a strategy at its turn boundary. Draining ALL held messages means
        none is left behind; entering them only between turns means one can never
        land between an assistant tool_use and its tool_result. A held message is
        ``user``-role data, never system (LLM01/ASI06), and each entry is attributed
        to its ``caller_did`` in the tamper-evident chain (Audit pillar). ``steer``
        is drained before ``follow_up`` so a message that asked to arrive sooner
        keeps its place; arcrun makes no trust decision here.
        """
        for queue in (self.steer_queue, self.followup_queue):
            while not queue.empty():
                injection = queue.get_nowait()
                self.messages.append(user_message(injection.message))
                self.event_bus.emit(
                    "message.injected",
                    {
                        "caller_did": injection.caller_did,
                        "message_id": injection.message_id,
                        "preview": injection.preview_text[:_HELD_PREVIEW_LEN],
                    },
                )
