"""SessionRouter — per-(user, agent) session management with race-condition guard.

CRITICAL DESIGN NOTE (Hermes PR #4926):
========================================
The most dangerous concurrency bug in gateway implementations is the
"pre-await race" — two messages from the same user arrive at virtually
the same time. Without a synchronous guard, both coroutines can pass the
``if session_key in self._active_sessions`` check before either has had a
chance to insert into the dict, spawning two competing agent tasks for the
same session.

FIX: Insert into ``_active_sessions`` SYNCHRONOUSLY (before any await)
in the same event-loop tick as the guard check. Python's asyncio guarantees
that no other coroutine runs between two synchronous statements within the
same task. Only an explicit ``await`` is a preemption point.

WRONG (introduces a race window):
    if session_key not in self._active_sessions:
        await something()          # <-- another message can pass the check here
        self._active_sessions[session_key] = asyncio.Event()

CORRECT (no await between check and assignment):
    if session_key in self._active_sessions:
        self._queue_for_session(session_key, event)
        return
    self._active_sessions[session_key] = asyncio.Event()  # SYNC — no await above
    asyncio.create_task(self._process_session(session_key, event))

The integration test ``tests/integration/test_race_regression.py`` fires
N=20 concurrent messages at the same session key and asserts exactly
one agent task spawned.

DM Pairing Interceptor (T1.8):
================================
Before routing any event to the agent executor, SessionRouter checks whether
the user is in the allowlist (via the composed PairingInterceptor). If not,
the message is intercepted: a pairing code is minted and DM'd to the user via
the adapter_map, and the event is dropped (not routed to the agent).

The interceptor is a no-op when ``pairing_store=None`` (default), allowing
the gateway to run without pairing enforcement during development or testing.

Queue management (T1.8 / SPEC-018):
=====================================
Per-session queues are managed by the composed QueueManager, which enforces
a bounded depth (max 100) and idle TTL eviction (1h) to prevent unbounded
growth. Test-hook counters (agent_tasks_spawned, queued_events) are retained
for backward-compatibility with existing tests.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

from arcgateway.delivery import DeliveryTarget
from arcgateway.executor import Delta, Executor, InboundEvent
from arcgateway.session_pairing import PairingInterceptor
from arcgateway.session_queue import QueueManager
from arcgateway.stream_bridge import StreamBridge
from arcgateway.telemetry import hash_user_did

if TYPE_CHECKING:
    # IdentityGraph is an optional integration dep; guard prevents circular import.
    from arcagent.modules.session.identity_graph import IdentityGraph

    from arcgateway.adapters.base import BasePlatformAdapter

_logger = logging.getLogger("arcgateway.session")


@runtime_checkable
class _AdapterProtocol(Protocol):
    """Minimum adapter surface required by SessionRouter / StreamBridge.

    Defined here so ``_adapter`` can be typed precisely without importing
    ``BasePlatformAdapter`` at module level (which would create a hard dep
    on arcgateway.adapters at import time).

    ``send_with_id`` is optional — StreamBridge detects its presence via
    ``hasattr`` and falls back to ``send`` when absent.
    """

    async def send(self, target: DeliveryTarget, message: str) -> None:
        """Deliver a complete message to the target."""
        ...

    async def send_with_id(self, target: DeliveryTarget, message: str) -> str | None:
        """Deliver a message and return a platform message ID for later edits.

        Optional extension method.  StreamBridge uses ``hasattr`` to probe for
        this before calling it; adapters that do not support message editing
        need not implement it.
        """
        ...


def build_session_key(agent_did: str, user_did: str) -> str:
    """Build a deterministic 16-hex-char session key from (agent, user) pair.

    Same (agent, user) pair always produces the same key, regardless of which
    platform the user messaged from — enabling cross-platform session continuity
    (D-06 and SDD §3.3).

    The key is a truncated SHA-256 digest. Truncation to 16 chars is intentional:
    collision probability is negligible for expected concurrency levels (~2^64
    preimage resistance) while keeping session keys human-readable in logs.

    Args:
        agent_did: The target agent's DID (e.g. "did:arc:org:agent/id").
        user_did: The resolved cross-platform user DID.

    Returns:
        16-character lowercase hex string.
    """
    digest = hashlib.sha256(f"{agent_did}:{user_did}".encode()).hexdigest()
    return digest[:16]



class SessionRouter:
    """Routes inbound events to per-session agent tasks.

    Each unique (agent_did, user_did) pair maps to exactly one active session
    at a time. Concurrent inbound messages for the same session are queued
    in a per-session FIFO (via QueueManager) and replayed sequentially after
    the active turn completes.

    Pairing interceptor: Composed via PairingInterceptor. Messages from users
    NOT in the allowlist are intercepted BEFORE session routing. The user
    receives a one-time pairing code via DM and must await operator approval.
    Intercepted messages are silently dropped (not queued) — the user must
    re-send after pairing.

    Thread safety: SessionRouter is NOT thread-safe. It is designed for
    single-threaded asyncio use. All state mutations happen in synchronous
    code between awaits, which asyncio's cooperative scheduling guarantees
    will not be interrupted.

    Attributes:
        _executor:         Executor implementation to run agent tasks.
        _active_sessions:  Maps session_key → asyncio.Event set when the
                           active turn completes.
        _queue_mgr:        QueueManager for per-session bounded FIFO queues.
        _pending_tasks:    Strong references to spawned asyncio.Tasks.
        _pairing:          PairingInterceptor for DM pairing enforcement.
        agent_tasks_spawned: Counter for testing (test-hooks only).
        queued_events:     Snapshot for testing (test-hooks only).
    """

    def __init__(
        self,
        executor: Executor,
        *,
        pairing_store: object | None = None,
        user_allowlist: set[str] | None = None,
        pairing_db_path: Path | None = None,
        identity_graph: IdentityGraph | None = None,
        adapter: BasePlatformAdapter | None = None,
        adapter_map: dict[str, BasePlatformAdapter] | None = None,
        delivery_target_factory: Any | None = None,
        _test_hooks: bool = True,
    ) -> None:
        """Initialise SessionRouter with the given executor.

        Args:
            executor:       Executor implementation (AsyncioExecutor, etc.).
            pairing_store:  Optional PairingStore instance.
            user_allowlist: Set of approved user_did values. None = all approved.
            pairing_db_path: Convenience arg — auto-creates a PairingStore at path.
            identity_graph:  Optional IdentityGraph for cross-platform identity
                             resolution (D-06 / SDD §3.3).
            adapter:        Optional primary platform adapter for StreamBridge delivery.
            adapter_map:    Optional platform→adapter map for pairing DM delivery.
                            When provided, PairingInterceptor uses it to deliver codes.
            delivery_target_factory: Optional callable
                            ``(event: InboundEvent) -> DeliveryTarget``.
            _test_hooks:    When True (default), maintains agent_tasks_spawned and
                            queued_events dicts for test introspection.
        """
        self._executor = executor
        self._identity_graph: object | None = identity_graph
        self._test_hooks = _test_hooks

        # SYNCHRONOUS GUARD STATE — never modify these inside an await.
        # session_key → asyncio.Event (set when current turn completes)
        self._active_sessions: dict[str, asyncio.Event] = {}

        # Strong references to spawned tasks — prevents GC before completion.
        self._pending_tasks: set[asyncio.Task[None]] = set()

        # StreamBridge adapter wiring (optional — no-op when None).
        self._adapter: _AdapterProtocol | None = adapter  # type: ignore[assignment]
        self._delivery_target_factory = delivery_target_factory
        self._stream_bridge = StreamBridge()

        # Composed pairing interceptor (T1.8).
        self._pairing = PairingInterceptor(
            user_allowlist=user_allowlist,
            pairing_store=pairing_store,
            pairing_db_path=pairing_db_path,
            adapter_map=adapter_map,
        )

        # Composed queue manager with bounded depth + idle eviction.
        self._queue_mgr = QueueManager()

        # Test-hook counters (preserved for backward-compat with existing tests).
        # Production code must NOT gate logic on these.
        self.agent_tasks_spawned: dict[str, int] = {}
        self.queued_events: dict[str, list[InboundEvent]] = {}

    # -----------------------------------------------------------------------
    # Allowlist delegation (public API — callers reference SessionRouter)
    # -----------------------------------------------------------------------

    def add_approved_user(self, user_did: str) -> None:
        """Add a user DID to the allowlist (called after pairing approval).

        Args:
            user_did: The DID of the newly approved user.
        """
        self._pairing.add_approved_user(user_did)
        # Also emit via the session logger for backward-compat with existing tests
        # that capture arcgateway.session. PairingInterceptor emits the same event
        # via arcgateway.session_pairing.
        _logger.info("Pairing: user uid_h=%s added to allowlist", hash_user_did(user_did))

    def remove_approved_user(self, user_did: str) -> None:
        """Remove a user DID from the allowlist (e.g. on ban or re-pair).

        Args:
            user_did: The DID to remove.
        """
        self._pairing.remove_approved_user(user_did)

    # -----------------------------------------------------------------------
    # Core routing
    # -----------------------------------------------------------------------

    async def handle(self, event: InboundEvent) -> None:
        """Route an inbound event to its session.

        This is the primary entry point called by platform adapters after
        they normalise a platform-specific message into an InboundEvent.

        Pairing intercept runs BEFORE the session-key guard. If the user is
        not in the allowlist, the message is intercepted (code minted and DM'd)
        and this method returns WITHOUT routing to the agent.

        RACE-CONDITION GUARD: The check-and-assign of ``_active_sessions``
        is SYNCHRONOUS. No ``await`` occurs between the guard check and the
        dict assignment. See module docstring for full explanation.

        Args:
            event: Normalised inbound event from a platform adapter.
        """
        # --- Identity graph resolution (T1.3 / SDD §3.3) ---
        # Synchronous (SQLite read) — no await before the race guard.
        if self._identity_graph is not None:
            resolved_did = self._resolve_user_did(event.platform, event.user_did)
            if resolved_did != event.user_did:
                event = event.model_copy(update={
                    "user_did": resolved_did,
                    "session_key": build_session_key(event.agent_did, resolved_did),
                })

        # --- Pairing interceptor (T1.8) ---
        if not self._pairing.is_user_approved(event.user_did):
            await self._pairing.handle_unpaired_user(event)
            return

        session_key = event.session_key

        # CRITICAL: if-block and dict assignment below are synchronous.
        # No await may appear between the guard check and the assignment.
        if session_key in self._active_sessions:
            self._queue_for_session(session_key, event)
            return

        # Mark the session as active BEFORE any await.
        done_event = asyncio.Event()
        self._active_sessions[session_key] = done_event

        # Initialise per-session bookkeeping.
        self._queue_mgr.ensure_session(session_key)
        if self._test_hooks:
            if session_key not in self.agent_tasks_spawned:
                self.agent_tasks_spawned[session_key] = 0
                self.queued_events[session_key] = []
            self.agent_tasks_spawned[session_key] += 1

        task = asyncio.create_task(
            self._process_session(session_key, event, done_event),
            name=f"session:{session_key}",
        )
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    # -----------------------------------------------------------------------
    # Private session processing
    # -----------------------------------------------------------------------

    def _queue_for_session(self, session_key: str, event: InboundEvent) -> None:
        """Append event to the per-session FIFO queue (via QueueManager).

        Called synchronously from handle() when a session is already active.

        Args:
            session_key: Unique session identifier.
            event: Event to enqueue.
        """
        enqueued = self._queue_mgr.enqueue(session_key, event)
        if enqueued and self._test_hooks:
            self.queued_events[session_key] = self._queue_mgr.snapshot(session_key)

    async def _process_session(
        self,
        session_key: str,
        event: InboundEvent,
        done_event: asyncio.Event,
    ) -> None:
        """Execute one turn and then drain the queue for this session.

        Runs as an asyncio.Task. Processes the triggering event, then
        checks whether new messages were queued while the turn was in
        flight. If so, processes them sequentially (one turn at a time).

        Args:
            session_key: Session being processed.
            event: The triggering event for this turn.
            done_event: asyncio.Event set on completion of this turn.
        """
        try:
            await self._run_turn(session_key, event)
        except Exception:
            _logger.exception("Unhandled error in session %s turn", session_key)
        finally:
            done_event.set()

        await self._drain_queue(session_key)

    async def _run_turn(self, session_key: str, event: InboundEvent) -> None:
        """Execute a single agent turn via the executor.

        When an adapter is wired, forwards deltas via StreamBridge.consume()
        for per-token progressive delivery with 3-strikes flood-control.
        When no adapter is wired, logs deltas for observability (dev/test mode).

        Args:
            session_key: Session being processed.
            event: Inbound event to execute.
        """
        _logger.info(
            "Session %s: turn start platform=%s uid_h=%s",
            session_key, event.platform, hash_user_did(event.user_did),
        )
        try:
            delta_stream: AsyncIterator[Delta] = await self._executor.run(event)

            if self._adapter is not None:
                target = self._resolve_delivery_target(event)
                await self._stream_bridge.consume(delta_stream, target, self._adapter)
            else:
                async for delta in delta_stream:
                    if delta.is_final:
                        _logger.debug("Session %s: turn complete", session_key)
                    else:
                        _logger.debug(
                            "Session %s: delta kind=%s content=%r",
                            session_key,
                            delta.kind,
                            delta.content[:80] if delta.content else "",
                        )
        except Exception:
            _logger.exception("Executor error in session %s", session_key)
            raise

    def _resolve_delivery_target(self, event: InboundEvent) -> DeliveryTarget:
        """Build a DeliveryTarget from an InboundEvent.

        Args:
            event: Inbound event to derive a target from.

        Returns:
            DeliveryTarget for the event's platform chat.
        """
        if self._delivery_target_factory is not None:
            return cast("DeliveryTarget", self._delivery_target_factory(event))
        return DeliveryTarget.parse(f"{event.platform}:{event.chat_id}")

    async def _drain_queue(self, session_key: str) -> None:
        """Process queued events sequentially after the active turn completes.

        Args:
            session_key: Session whose queue to drain.
        """
        queue = self._queue_mgr.peek_queue(session_key)
        if not queue:
            self._active_sessions.pop(session_key, None)
            return

        while queue:
            next_event = queue.popleft()
            if self._test_hooks:
                self.queued_events[session_key] = list(queue)

            done_event = asyncio.Event()
            self._active_sessions[session_key] = done_event
            if self._test_hooks:
                self.agent_tasks_spawned[session_key] += 1

            try:
                await self._run_turn(session_key, next_event)
            except Exception:
                _logger.exception(
                    "Unhandled error draining queue for session %s", session_key
                )
            finally:
                done_event.set()

        self._active_sessions.pop(session_key, None)

    def _resolve_user_did(self, platform: str, raw_user_did: str) -> str:
        """Resolve a platform-scoped user_did to a stable cross-platform DID.

        Args:
            platform: Source platform name (e.g. "telegram", "slack").
            raw_user_did: Adapter-supplied DID.

        Returns:
            Stable cross-platform user DID, or raw_user_did if not resolvable.
        """
        if self._identity_graph is None:
            return raw_user_did

        prefix = f"did:arc:{platform}:"
        if raw_user_did.startswith(prefix):
            platform_user_id = raw_user_did[len(prefix):]
        elif ":" in raw_user_did:
            platform_user_id = raw_user_did.split(":", 1)[-1]
        else:
            platform_user_id = raw_user_did

        try:
            _graph = cast(Any, self._identity_graph)
            resolved: str = _graph.resolve_user_identity(platform, platform_user_id)
            return resolved
        except Exception:
            _logger.exception(
                "SessionRouter: identity graph resolution failed for %s:%s",
                platform,
                platform_user_id,
            )
            return raw_user_did

    # -----------------------------------------------------------------------
    # Observability helpers
    # -----------------------------------------------------------------------

    def active_session_count(self) -> int:
        """Return the number of currently active sessions."""
        return len(self._active_sessions)

    def queue_depth(self, session_key: str) -> int:
        """Return the number of queued (pending) events for a session."""
        return self._queue_mgr.depth(session_key)
