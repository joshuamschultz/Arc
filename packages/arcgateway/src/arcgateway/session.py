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
the user is in the allowlist. If not, the message is intercepted:

1. If the user has NO pending pairing code: mint a new code, DM it to the user
   via the adapter, return (do NOT route to agent).
2. If the user HAS a pending pairing code: re-send the pending code reminder,
   return (still do NOT route to agent — the user must pair first).

Once a code is approved (via ``arc gateway pair approve <code>``), the operator
adds the user to the allowlist and subsequent messages route normally.

The interceptor is a no-op when ``pairing_store=None`` (default), allowing
the gateway to run without pairing enforcement during development or testing.
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from arcgateway.executor import Delta, Executor, InboundEvent

if TYPE_CHECKING:
    # IdentityGraph is an optional integration dep; guard prevents circular import.
    from arcagent.modules.session.identity_graph import IdentityGraph

_logger = logging.getLogger("arcgateway.session")


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
    in a per-session FIFO and replayed sequentially after the active turn
    completes.

    Pairing interceptor: If a PairingStore is provided, messages from users
    NOT in the allowlist are intercepted BEFORE session routing. The user
    receives a one-time pairing code via DM and must await operator approval.
    Intercepted messages are silently dropped (not queued) — the user must
    re-send after pairing. This mirrors Hermes gateway/pairing.py behaviour.

    Thread safety: SessionRouter is NOT thread-safe. It is designed for
    single-threaded asyncio use. All state mutations happen in synchronous
    code between awaits, which asyncio's cooperative scheduling guarantees
    will not be interrupted.

    Attributes:
        _executor: Executor implementation to run agent tasks.
        _active_sessions: Maps session_key → asyncio.Event that is set when
            the active turn completes (allowing the queue-drainer to proceed).
        _queues: Per-session FIFO of queued InboundEvents.
        _pending_tasks: Strong references to spawned asyncio.Tasks, preventing
            premature garbage collection before they complete.
        _pairing_store: Optional PairingStore for DM pairing enforcement.
        _user_allowlist: Set of approved user_did values. When None, all users
            are treated as approved (no pairing enforcement).
        agent_tasks_spawned: Counter for testing — maps session_key → int.
        queued_events: Snapshot of per-session queues for testing.
    """

    def __init__(
        self,
        executor: Executor,
        *,
        pairing_store: object | None = None,
        user_allowlist: set[str] | None = None,
        pairing_db_path: Path | None = None,
        identity_graph: IdentityGraph | None = None,
    ) -> None:
        """Initialise SessionRouter with the given executor.

        Args:
            executor:       Executor implementation (AsyncioExecutor, etc.).
            pairing_store:  Optional PairingStore instance. When provided,
                            messages from unknown users are intercepted and a
                            one-time pairing code is DM'd to them.
            user_allowlist: Set of approved user_did values. None = all approved.
            pairing_db_path: Convenience arg — if provided and pairing_store is
                             None, a PairingStore is created at this path. Ignored
                             if pairing_store is explicitly passed.
            identity_graph:  Optional IdentityGraph instance.  When provided,
                             each inbound event's user_did is resolved through
                             the graph before the session key is computed —
                             enabling cross-platform identity unification (D-06).
                             When None, the adapter-supplied user_did is used as-is.
        """
        self._executor = executor
        # Cross-platform identity resolution (SDD §3.3 / T1.3).
        # Stored as Any so we don't hard-depend on arcagent at import time;
        # arcagent is an optional integration dep for arcgateway.
        self._identity_graph: object | None = identity_graph

        # SYNCHRONOUS GUARD STATE — never modify these inside an await.
        # session_key → asyncio.Event (set when current turn completes)
        self._active_sessions: dict[str, asyncio.Event] = {}

        # session_key → deque of queued events (FIFO)
        self._queues: dict[str, collections.deque[InboundEvent]] = {}

        # Strong references to spawned tasks — prevents GC before completion.
        # Tasks remove themselves on completion via the done callback.
        self._pending_tasks: set[asyncio.Task[None]] = set()

        # Pairing enforcement (T1.8 — optional; no-op when None)
        self._pairing_store: object | None
        if pairing_store is not None:
            self._pairing_store = pairing_store
        elif pairing_db_path is not None:
            # Lazy import to avoid circular dependency at module load time
            from arcgateway.pairing import PairingStore

            self._pairing_store = PairingStore(db_path=pairing_db_path)
        else:
            self._pairing_store = None

        # When None, all users are approved (pairing enforcement disabled)
        self._user_allowlist: set[str] | None = user_allowlist

        # Introspection hooks for testing (see test_race_regression.py)
        self.agent_tasks_spawned: dict[str, int] = {}
        self.queued_events: dict[str, list[InboundEvent]] = {}

    def add_approved_user(self, user_did: str) -> None:
        """Add a user DID to the allowlist (called after pairing approval).

        Args:
            user_did: The DID of the newly approved user.
        """
        if self._user_allowlist is None:
            self._user_allowlist = set()
        self._user_allowlist.add(user_did)
        _logger.info("Pairing: user %r added to allowlist", user_did)

    def remove_approved_user(self, user_did: str) -> None:
        """Remove a user DID from the allowlist (e.g. on ban or re-pair).

        Args:
            user_did: The DID to remove.
        """
        if self._user_allowlist is not None:
            self._user_allowlist.discard(user_did)

    def _is_user_approved(self, user_did: str) -> bool:
        """Return True if the user is approved or pairing enforcement is disabled.

        Args:
            user_did: The user's DID to check.

        Returns:
            True if approved or allowlist is None (enforcement disabled).
        """
        if self._user_allowlist is None:
            return True  # No pairing enforcement active
        return user_did in self._user_allowlist

    async def handle(self, event: InboundEvent) -> None:
        """Route an inbound event to its session.

        This is the primary entry point called by platform adapters after
        they normalise a platform-specific message into an InboundEvent.

        Pairing intercept runs BEFORE the session-key guard. If the user is
        not in the allowlist, the message is intercepted (code minted and DM'd)
        and this method returns WITHOUT routing to the agent. Both "no pending
        code" and "already has pending code" cases are intercepted — in neither
        case should the unapproved user reach the agent.

        RACE-CONDITION GUARD: The check-and-assign of ``_active_sessions``
        is SYNCHRONOUS. No ``await`` occurs between the guard check and the
        dict assignment. See module docstring for full explanation.

        Args:
            event: Normalised inbound event from a platform adapter.
        """
        # --- Identity graph resolution (T1.3 / SDD §3.3) ---
        # Resolve the platform-scoped user_did to a stable cross-platform DID
        # BEFORE the pairing check and race guard.  This call is SYNCHRONOUS
        # (SQLite read) so it does not introduce an await before the guard.
        # The resolved DID replaces event.user_did and event.session_key so
        # subsequent routing uses the canonical cross-platform identity.
        if self._identity_graph is not None:
            resolved_did = self._resolve_user_did(event.platform, event.user_did)
            if resolved_did != event.user_did:
                event = event.model_copy(update={
                    "user_did": resolved_did,
                    "session_key": build_session_key(event.agent_did, resolved_did),
                })

        # --- Pairing interceptor (T1.8) ---
        # Runs before session routing. Approved users pass through immediately.
        if not self._is_user_approved(event.user_did):
            await self._handle_unpaired_user(event)
            return  # Do NOT route to agent — user must pair first

        session_key = event.session_key

        # CRITICAL: This if-block and the dict assignment below are synchronous.
        # No await may appear between the guard check and the assignment.
        if session_key in self._active_sessions:
            # A turn is already in flight — queue the new event for sequential replay.
            self._queue_for_session(session_key, event)
            return

        # Mark the session as active BEFORE any await.
        # asyncio guarantees: no other coroutine runs between this statement
        # and the preceding if-check (both are synchronous, no yield point).
        done_event = asyncio.Event()
        self._active_sessions[session_key] = done_event

        # Initialise per-session bookkeeping if this is the first message.
        if session_key not in self._queues:
            self._queues[session_key] = collections.deque()
            self.queued_events[session_key] = []
            self.agent_tasks_spawned[session_key] = 0

        # Increment spawn counter BEFORE create_task so the test assertion
        # sees the correct count even if the task runs immediately.
        self.agent_tasks_spawned[session_key] += 1

        # Spawn the agent task. create_task schedules it on the event loop
        # but does NOT run it immediately — control returns here first.
        # Store a strong reference to prevent GC before the task completes.
        task = asyncio.create_task(
            self._process_session(session_key, event, done_event),
            name=f"session:{session_key}",
        )
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    async def _handle_unpaired_user(self, event: InboundEvent) -> None:
        """Intercept a message from an unapproved user and issue a pairing code.

        Mints a new pairing code (or reminds the user of their existing pending
        code) and logs the interception. The actual DM delivery to the user is
        delegated to the adapter via event metadata — the adapter is responsible
        for calling ``adapter.send(event.chat_id, message)`` when it receives
        the pairing DM trigger.

        TODO(M1 T1.7 integration): Wire the DM response back through the adapter.
        Currently logs the action. Real delivery requires the adapter reference.
        See SDD §3.1 DM Pairing and runner.py integration.

        Args:
            event: The intercepted inbound event from an unapproved user.
        """
        if self._pairing_store is None:
            # Pairing store configured but not wired — log and silently drop.
            _logger.warning(
                "Pairing: user %r is not in allowlist but no pairing_store configured "
                "— message dropped (platform=%s)",
                event.user_did,
                event.platform,
            )
            return

        # Import here to keep top-level import graph clean
        from arcgateway.pairing import (
            PairingPlatformFull,
            PairingPlatformLocked,
            PairingRateLimited,
            PairingStore,
        )

        if not isinstance(self._pairing_store, PairingStore):
            _logger.error(
                "Pairing: pairing_store is not a PairingStore instance — skipping"
            )
            return

        try:
            pairing_code = await self._pairing_store.mint_code(
                platform=event.platform,
                platform_user_id=event.user_did,
            )
            _logger.info(
                "Pairing: minted code for user %r on platform %r "
                "(code_id hidden — see audit log)",
                event.user_did,
                event.platform,
            )
            # TODO(M1 T1.7 integration): Deliver code via adapter.send():
            #   await adapter.send(
            #       event.chat_id,
            #       f"To pair with this agent, share this code with your operator: "
            #       f"{pairing_code.code}\n\nCode expires in 1 hour.",
            #   )
            # The pairing_code object is intentionally not logged here (it's a secret).
            del pairing_code  # Prevent accidental log inclusion via __repr__

        except PairingRateLimited:
            _logger.info(
                "Pairing: user %r already has a pending code on platform %r "
                "— reminding (rate limited)",
                event.user_did,
                event.platform,
            )
            # TODO(M1 T1.7 integration): Re-send reminder via adapter.send():
            #   await adapter.send(
            #       event.chat_id,
            #       "You already have a pending pairing code. "
            #       "Please share it with your operator or wait for it to expire.",
            #   )

        except PairingPlatformFull:
            _logger.warning(
                "Pairing: platform %r has too many pending codes — user %r dropped",
                event.platform,
                event.user_did,
            )
            # TODO(M1 T1.7 integration): Optionally notify user that pairing is busy.

        except PairingPlatformLocked:
            _logger.warning(
                "Pairing: platform %r is locked — user %r dropped",
                event.platform,
                event.user_did,
            )
            # TODO(M1 T1.7 integration): Optionally notify user that pairing is locked.

    def _queue_for_session(self, session_key: str, event: InboundEvent) -> None:
        """Append event to the per-session FIFO queue.

        Called synchronously from handle() when a session is already active.
        Initialises the queue structures on first use.

        Args:
            session_key: Unique session identifier.
            event: Event to enqueue.
        """
        if session_key not in self._queues:
            self._queues[session_key] = collections.deque()
            self.queued_events[session_key] = []
            self.agent_tasks_spawned[session_key] = 0

        self._queues[session_key].append(event)
        # Keep testing snapshot in sync
        self.queued_events[session_key] = list(self._queues[session_key])

        _logger.debug(
            "Session %s busy — queued event (queue depth=%d)",
            session_key,
            len(self._queues[session_key]),
        )

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

        Clears the session from ``_active_sessions`` when the queue is
        empty so the next inbound message starts a fresh task.

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

        # Drain any messages that arrived while we were running.
        await self._drain_queue(session_key)

    async def _run_turn(self, session_key: str, event: InboundEvent) -> None:
        """Execute a single agent turn via the executor.

        Collects all deltas from the executor and (in real integration)
        forwards them via StreamBridge → Adapter.send(). In the skeleton,
        deltas are logged for observability.

        TODO (M1 final integration): Connect to StreamBridge for real
        platform delivery. See SDD §3.1 Stream Flood-Control.

        Args:
            session_key: Session being processed.
            event: Inbound event to execute.
        """
        _logger.info(
            "Session %s: starting turn (platform=%s user=%s)",
            session_key,
            event.platform,
            event.user_did,
        )
        try:
            delta_stream: AsyncIterator[Delta] = await self._executor.run(event)
            async for delta in delta_stream:
                if delta.is_final:
                    _logger.debug("Session %s: turn complete", session_key)
                else:
                    # TODO: forward delta to StreamBridge → Adapter.send()
                    _logger.debug(
                        "Session %s: delta kind=%s content=%r",
                        session_key,
                        delta.kind,
                        delta.content[:80] if delta.content else "",
                    )
        except Exception:
            _logger.exception("Executor error in session %s", session_key)
            raise

    async def _drain_queue(self, session_key: str) -> None:
        """Process queued events sequentially after the active turn completes.

        Keeps the session active (in ``_active_sessions``) while there are
        queued messages. Only removes the session when the queue is empty,
        allowing the next inbound message to start a fresh task.

        Args:
            session_key: Session whose queue to drain.
        """
        queue = self._queues.get(session_key)
        if not queue:
            # No pending messages — release the session slot.
            self._active_sessions.pop(session_key, None)
            return

        while queue:
            next_event = queue.popleft()
            # Update testing snapshot
            self.queued_events[session_key] = list(queue)

            done_event = asyncio.Event()
            self._active_sessions[session_key] = done_event
            self.agent_tasks_spawned[session_key] += 1

            try:
                await self._run_turn(session_key, next_event)
            except Exception:
                _logger.exception(
                    "Unhandled error draining queue for session %s", session_key
                )
            finally:
                done_event.set()

        # Queue drained — release the session slot.
        self._active_sessions.pop(session_key, None)

    def _resolve_user_did(self, platform: str, raw_user_did: str) -> str:
        """Resolve a platform-scoped user_did to a stable cross-platform DID.

        Delegates to IdentityGraph.resolve_user_identity() when an identity
        graph is configured.  Returns raw_user_did unchanged when no graph is
        available — callers may rely on this being a pure function for any
        given (platform, raw_user_did) input.

        The call is synchronous (SQLite) so it is safe to place before the
        asyncio race-condition guard in handle().

        Args:
            platform: Source platform name (e.g. "telegram", "slack").
            raw_user_did: Adapter-supplied DID (e.g. "did:arc:telegram:12345").

        Returns:
            Stable cross-platform user DID, or raw_user_did if not resolvable.
        """
        if self._identity_graph is None:
            return raw_user_did

        # identity_graph is stored as object | None to avoid hard import at
        # module level.  We call the method by name — if the object does not
        # have resolve_user_identity the AttributeError propagates intentionally.
        # Extract the raw platform-native user ID from the DID if possible.
        # Adapters use the convention "did:arc:{platform}:{user_id}".
        prefix = f"did:arc:{platform}:"
        if raw_user_did.startswith(prefix):
            platform_user_id = raw_user_did[len(prefix):]
        elif ":" in raw_user_did:
            # Slack convention: "slack:{user_id}"
            platform_user_id = raw_user_did.split(":", 1)[-1]
        else:
            platform_user_id = raw_user_did

        try:
            # _identity_graph is stored as object|None to avoid hard import at module level.
            # cast to Any so we can call resolve_user_identity without an attr-defined error.
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

    def active_session_count(self) -> int:
        """Return the number of currently active sessions."""
        return len(self._active_sessions)

    def queue_depth(self, session_key: str) -> int:
        """Return the number of queued (pending) events for a session."""
        q = self._queues.get(session_key)
        return len(q) if q is not None else 0
