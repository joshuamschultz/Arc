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
growth. Test-instrumentation counters (agent_tasks_spawned, queued_events)
are exposed for tests that assert on per-session task and queue behaviour;
production code must not gate logic on them.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

from arcgateway.commands import CommandRegistry, build_default_registry
from arcgateway.delivery import DeliveryTarget
from arcgateway.executor import Delta, Executor, InboundEvent
from arcgateway.session_epoch import SessionEpochStore
from arcgateway.session_pairing import PairingInterceptor
from arcgateway.session_queue import QueueManager
from arcgateway.stream_bridge import StreamBridge
from arcgateway.telemetry import emit_audit, hash_user_did

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


def build_session_key(agent_did: str, user_did: str, *, generation: int = 0) -> str:
    """Build a deterministic 16-hex-char session key from (agent, user) pair.

    Same (agent, user) pair always produces the same key, regardless of which
    platform the user messaged from — enabling cross-platform session continuity
    (D-06 and SDD §3.3).

    The key is a truncated SHA-256 digest. Truncation to 16 chars is intentional:
    collision probability is negligible for expected concurrency levels (~2^64
    preimage resistance) while keeping session keys human-readable in logs.

    ``generation`` folds a per-(agent, user) rotation counter into the key so a
    ``/new`` command can mint a fresh, empty session (see SessionEpochStore).
    ``generation=0`` reproduces the original key exactly — the correct default
    that keeps every existing on-disk session valid.

    Args:
        agent_did: The target agent's DID (e.g. "did:arc:org:agent/id").
        user_did: The resolved cross-platform user DID.
        generation: Session rotation counter; 0 is the first/plain session.

    Returns:
        16-character lowercase hex string.
    """
    base = f"{agent_did}:{user_did}"
    seed = base if generation == 0 else f"{base}:g{generation}"
    return hashlib.sha256(seed.encode()).hexdigest()[:16]


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
        command_registry: CommandRegistry | None = None,
        session_epoch_db_path: Path | None = None,
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

        # Outbound channel registry — adapter.name → adapter. A reply is
        # delivered through the adapter that owns the event's source channel,
        # resolved generically by name (see _resolve_outbound). The router
        # never references a specific platform; all send/edit/typing specifics
        # live in the adapter packages. Seeded from the legacy single `adapter`
        # arg and `adapter_map`; this same registry also serves pairing DMs.
        self._adapters: dict[str, _AdapterProtocol] = {}
        if adapter is not None:
            self._adapters[adapter.name] = adapter
        if adapter_map:
            self._adapters.update(adapter_map)
        self._delivery_target_factory = delivery_target_factory
        self._stream_bridge = StreamBridge()

        # Composed pairing interceptor (T1.8) — shares the same channel registry.
        self._pairing = PairingInterceptor(
            user_allowlist=user_allowlist,
            pairing_store=pairing_store,
            pairing_db_path=pairing_db_path,
            adapter_map=self._adapters,
        )

        # Composed queue manager with bounded depth + idle eviction.
        self._queue_mgr = QueueManager()

        # Test instrumentation. Tests assert on per-session task spawn counts
        # and queued-event snapshots; production code must NOT gate logic on these.
        self.agent_tasks_spawned: dict[str, int] = {}
        self.queued_events: dict[str, list[InboundEvent]] = {}

        # Slash-command registry + session rotation. The registry is the one
        # cross-platform command surface (every adapter delivers "/cmd" as
        # message text through handle()); the epoch store folds a per-(agent,
        # user) generation into the session key so /new mints a fresh session.
        self._commands = command_registry or build_default_registry()
        self._epochs = SessionEpochStore(session_epoch_db_path)

    # -----------------------------------------------------------------------
    # Allowlist delegation (public API — callers reference SessionRouter)
    # -----------------------------------------------------------------------

    def register_adapter(self, adapter: BasePlatformAdapter) -> None:
        """Register an adapter as the outbound channel for its platform.

        The router delivers a reply through the adapter whose ``name`` matches
        the inbound event's source (``event.platform``). Register every adapter
        the gateway runs (web, telegram, slack, …) so each platform's replies
        return to that platform — never another. Resolves the construction
        cycle: build the router first, build adapters with a closure over
        ``router.handle``, then ``router.register_adapter(adapter)`` for each.

        Idempotent: re-registering the same name replaces it (runtime swaps).
        """
        self._adapters[adapter.name] = adapter
        self._pairing.register_adapter(adapter.name, adapter)

    def set_adapter(self, adapter: BasePlatformAdapter) -> None:
        """Backwards-compatible alias for :meth:`register_adapter`."""
        self.register_adapter(adapter)

    def add_approved_user(self, user_did: str) -> None:
        """Add a user DID to the allowlist (called after pairing approval).

        Args:
            user_did: The DID of the newly approved user.
        """
        self._pairing.add_approved_user(user_did)
        # Dual-emit: both arcgateway.session and arcgateway.session_pairing log
        # the same approval event so tests can capture from either logger name.
        _logger.info("Pairing: user uid_h=%s added to allowlist", hash_user_did(user_did))

    def remove_approved_user(self, user_did: str) -> None:
        """Remove a user DID from the allowlist (e.g. on ban or re-pair).

        Args:
            user_did: The DID to remove.
        """
        self._pairing.remove_approved_user(user_did)

    # -----------------------------------------------------------------------
    # Session rotation (public API — /new command + arcui both call these)
    # -----------------------------------------------------------------------

    def current_session_key(self, agent_did: str, user_did: str) -> str:
        """Resolve the (agent, user) pair's *current* session key.

        Folds the pair's rotation generation into the deterministic key, so
        after a ``/new`` every surface converges on the same fresh session.
        """
        base = build_session_key(agent_did, user_did)
        generation = self._epochs.generation(base)
        return build_session_key(agent_did, user_did, generation=generation)

    def new_session(self, agent_did: str, user_did: str) -> str:
        """Rotate the (agent, user) session; return the new session key.

        Bumps the rotation generation so the next message hashes to a brand-new,
        empty session (``open_or_resume`` touches an empty log). The prior
        conversation is left intact on disk.
        """
        base = build_session_key(agent_did, user_did)
        generation = self._epochs.bump(base)
        key = build_session_key(agent_did, user_did, generation=generation)
        emit_audit(
            _logger,
            "gateway.session.rotated",
            {"uid_h": hash_user_did(user_did), "generation": generation},
        )
        return key

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
        # --- Identity + canonical session key (T1.3 / SDD §3.3, D-06) ---
        # The gateway core owns session-key policy: a filename-safe,
        # cross-platform-stable key derived from (agent, user). Adapters supply
        # platform identity only — they must NOT hand-craft the session key.
        # A raw "{agent_did}:{platform}:{chat_type}:{user}" string breaks the
        # executor's filename-safe key validator whenever the agent DID
        # contains '/' or ':' (e.g. did:arc:local:executor/abc). Deriving the
        # canonical key here makes every platform consistent and safe.
        # Synchronous (SQLite read) — no await before the race guard.
        resolved_did = event.user_did
        if self._identity_graph is not None:
            resolved_did = self._resolve_user_did(event.platform, event.user_did)
        canonical_key = self.current_session_key(event.agent_did, resolved_did)
        if resolved_did != event.user_did or event.session_key != canonical_key:
            event = event.model_copy(
                update={"user_did": resolved_did, "session_key": canonical_key}
            )

        # --- Pairing interceptor (T1.8) ---
        if not await self._pairing.is_user_approved(event.user_did, event.platform):
            await self._pairing.handle_unpaired_user(event)
            return

        # --- Slash-command interceptor ---
        # Registered commands (e.g. /new) are handled here and never reach the
        # session/executor machinery; an unknown "/token" falls through as
        # ordinary text. Runs AFTER pairing so an unapproved user cannot rotate
        # sessions or enumerate commands, and BEFORE the race guard so a command
        # never touches _active_sessions (preserving the pre-await race invariant).
        async def _reply(text: str) -> None:
            await self._send_reply(event, text)

        if await self._commands.dispatch(event, event.agent_did, resolved_did, self, _reply):
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
        except Exception:  # reason: fail-open — log + continue
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
            session_key,
            event.platform,
            hash_user_did(event.user_did),
        )
        try:
            delta_stream: AsyncIterator[Delta] = await self._executor.run(event)

            adapter = self._resolve_outbound(event)
            if adapter is not None:
                target = self._resolve_delivery_target(event)
                await self._stream_bridge.consume(delta_stream, target, adapter)
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
        except Exception:  # reason: re-raise after log
            _logger.exception("Executor error in session %s", session_key)
            raise

    async def dispatch_and_await(
        self,
        event: InboundEvent,
        *,
        timeout: float = 120.0,
    ) -> AsyncIterator[Delta]:
        """Request/response dispatch — push an event, stream deltas back.

        Companion to ``handle()`` for programmatic callers (FastAPI hosts,
        CLI demos, inter-agent orchestrators) that want the executor's
        full delta stream returned, not delivered via an adapter's
        ``send()``. Pairing and identity-graph resolution still run, so
        the same allowlist that gates platform messages also gates
        programmatic dispatch.

        Per-session serialisation: this method does NOT enqueue. The
        caller is responsible for not overlapping ``dispatch_and_await``
        calls for the same session_key — two concurrent calls will both
        invoke the executor and their delta streams will interleave.
        Most callers either route through a single agent at a time or
        wrap concurrent calls in their own ``asyncio.Lock``.

        Args:
            event: Inbound event (same shape ``handle()`` expects).
            timeout: Per-delta read timeout in seconds.

        Yields:
            Delta: from the executor in arrival order. Iteration ends on
            the executor's terminal ``Delta(kind="done", is_final=True)``.

        Raises:
            PermissionError: when the event's user_did is not on the
                pairing allowlist. Programmatic callers must pre-pair
                their user_dids (typically with ``add_approved_user``).
            asyncio.TimeoutError: when no delta arrives within ``timeout``.
        """
        # Identity-graph resolution (same step as handle()).
        if self._identity_graph is not None:
            resolved_did = self._resolve_user_did(event.platform, event.user_did)
            if resolved_did != event.user_did:
                event = event.model_copy(
                    update={
                        "user_did": resolved_did,
                        "session_key": build_session_key(event.agent_did, resolved_did),
                    }
                )

        if not await self._pairing.is_user_approved(event.user_did, event.platform):
            raise PermissionError(
                f"User {event.user_did!r} is not on the pairing allowlist; "
                "call add_approved_user() first for programmatic dispatch."
            )

        delta_stream = await self._executor.run(event)
        async for delta in delta_stream:
            yield delta
            if delta.is_final:
                return

    def _resolve_outbound(self, event: InboundEvent) -> _AdapterProtocol | None:
        """Resolve the outbound channel a reply should go to.

        Generic by design: the router matches the adapter's self-declared
        ``name`` to the event's source id (``event.platform``) so a reply
        returns to the platform it came from — Telegram answers on Telegram,
        Slack on Slack, web on web. The router holds NO platform-specific
        logic; every send/edit/typing detail lives in the adapter package.

        When exactly one channel is registered, deliver through it regardless
        of name (single-platform deployments and tests). When several are
        registered and none matches, there is no safe channel — the caller
        logs the turn instead of guessing.
        """
        adapter = self._adapters.get(event.platform)
        if adapter is None and len(self._adapters) == 1:
            return next(iter(self._adapters.values()))
        return adapter

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

    async def _send_reply(self, event: InboundEvent, text: str) -> None:
        """Deliver a standalone reply (e.g. a command response) to the source.

        Reuses the same outbound resolution as turn replies and pairing DMs.
        When no adapter owns the platform (e.g. programmatic dispatch with no
        registered channel), the reply is logged and dropped rather than guessed.
        """
        adapter = self._resolve_outbound(event)
        if adapter is None:
            _logger.warning(
                "command reply dropped — no outbound adapter for platform %s", event.platform
            )
            return
        await adapter.send(self._resolve_delivery_target(event), text)

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
            except Exception:  # reason: fail-open — log + continue
                _logger.exception("Unhandled error draining queue for session %s", session_key)
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
            platform_user_id = raw_user_did[len(prefix) :]
        elif ":" in raw_user_did:
            platform_user_id = raw_user_did.split(":", 1)[-1]
        else:
            platform_user_id = raw_user_did

        try:
            _graph = cast(Any, self._identity_graph)
            resolved: str = _graph.resolve_user_identity(platform, platform_user_id)
            return resolved
        except Exception:  # reason: fail-open — log + continue
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
