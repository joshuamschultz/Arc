"""SessionRouter — per-(user, agent) session routing.

WHERE TURNS ARE SERIALISED (Hermes PR #4926, SPEC-065 REQ-317):
================================================================
The most dangerous concurrency bug in gateway implementations is the
"pre-await race" — two messages from the same user arrive in the same
event-loop tick, both read "this session is idle", and both open a turn:
double replies, interleaved LLM context, duplicate audit events.

The guard against it is NOT here. The router hands every message to the
agent's delivery entry point and decides nothing about the run; the agent
serialises the decision per session and either joins the turn in flight or
opens a new one (arcagent ``SessionRunCoordinator.delivery``). One waiting
line, at the layer that owns turns.

The router therefore keeps no per-session FIFO of its own — a second waiting
line in front of the first only re-creates the drift it was meant to prevent.
A message is never held here: it is handed over, and the agent decides.

``tests/integration/test_race_regression.py`` fires N concurrent messages at
one session key through the real router, executor and agent and asserts
exactly one run opens while no message is lost.

DM Pairing Interceptor (T1.8):
================================
Before routing any event to the agent executor, SessionRouter checks whether
the user is in the allowlist (via the composed PairingInterceptor). If not,
the message is intercepted: a pairing code is minted and DM'd to the user via
the adapter_map, and the event is dropped (not routed to the agent).

The interceptor is a no-op when ``pairing_store=None`` (default), allowing
the gateway to run without pairing enforcement during development or testing.

The ``agent_tasks_spawned`` counter is test instrumentation only; production
code must not gate logic on it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

import arcagent

# Re-exported: arcui, arctui and the web adapter all reach session identity
# through this module, which is the gateway's single owner of it (D-678).
from arctrust.session_identity import build_session_key as build_session_key

from arcgateway.adapters.base import InboundDraft, PendingMedia
from arcgateway.commands import CommandRegistry, build_default_registry
from arcgateway.delivery import DeliveryTarget
from arcgateway.executor import Delta, Executor, InboundEvent
from arcgateway.media_custody import MediaCustodian
from arcgateway.media_store import MediaStore
from arcgateway.parts import Part, flatten_text
from arcgateway.session_epoch import SessionEpochStore
from arcgateway.session_pairing import PairingInterceptor
from arcgateway.stream_bridge import StreamBridge
from arcgateway.telemetry import emit_audit, hash_user_did

if TYPE_CHECKING:
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

    name: str
    """Platform id (e.g. "telegram"). Shared across bots of the same platform."""

    agent_did: str
    """DID of the agent this adapter's bot serves — disambiguates multiple bots
    on the same platform so a reply returns through the RIGHT bot."""

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


def _adapter_key(adapter: _AdapterProtocol) -> tuple[str, str]:
    """Outbound-registry key: (platform, agent_did). ``agent_did`` defaults to
    "" for single-bot adapters (web) that don't declare one."""
    return (adapter.name, getattr(adapter, "agent_did", "") or "")


def _draft_to_event(draft: InboundDraft) -> InboundEvent:
    """Turn an adapter's draft into the envelope the gateway routes.

    Only the parts that are already *here* survive the conversion — an artefact
    still sitting on the platform has no reference to carry, so it is added back
    by :class:`~arcgateway.media_custody.MediaCustodian` once the gateway has
    taken it. The result is that
    pairing and command dispatch see the message's words without a single byte
    having been downloaded.
    """
    words: list[Part] = [p for p in draft.parts if not isinstance(p, PendingMedia)]
    return InboundEvent(
        platform=draft.platform,
        chat_id=draft.chat_id,
        thread_id=draft.thread_id,
        user_did=draft.user_did,
        agent_did=draft.agent_did,
        message=flatten_text(words),
        parts=words,
        raw_payload=dict(draft.raw_payload),
    )


class SessionRouter:
    """Routes inbound events to the agent that owns their session.

    Each unique (agent_did, user_did) pair maps to one session key. Every
    inbound event is handed to the agent's delivery entry point on its own
    task; the agent decides whether the message joins the turn already in
    flight or opens a new one, and serialises that decision per session. The
    router holds no queue and makes no run decision (SPEC-065 COMP-006).

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
        _in_flight:        Maps session_key → count of handoff tasks still
                           running, for observability only.
        _pending_tasks:    Strong references to spawned asyncio.Tasks.
        _pairing:          PairingInterceptor for DM pairing enforcement.
        agent_tasks_spawned: Counter for testing (test-hooks only).
    """

    def __init__(
        self,
        executor: Executor,
        *,
        pairing_store: object | None = None,
        user_allowlist: set[str] | None = None,
        pairing_db_path: Path | None = None,
        identity_graph: arcagent.IdentityGraph | None = None,
        adapter: BasePlatformAdapter | None = None,
        adapter_map: dict[str, BasePlatformAdapter] | None = None,
        delivery_target_factory: Any | None = None,
        command_registry: CommandRegistry | None = None,
        session_epoch_db_path: Path | None = None,
        media_store_for: Callable[[str], MediaStore | None] | None = None,
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
            media_store_for: Resolves the MediaStore for one agent_did
                            (SPEC-065 COMP-002). Per-agent, not per-router: a
                            router serves the whole fleet but a workspace
                            belongs to ONE agent, so a single shared store
                            would write every agent's inbound artefacts into
                            one agent's home — breaking ADR-029 and putting one
                            agent's files inside another's readable workspace.
                            Returning None (or omitting this) means the gateway
                            has nowhere to write, so an artefact is announced to
                            the agent by name instead of being stored — the
                            message still arrives, minus the bytes.
            _test_hooks:    When True (default), maintains the agent_tasks_spawned
                            dict for test introspection.
        """
        self._executor = executor
        self._identity_graph: object | None = identity_graph
        self._custodian = MediaCustodian(
            media_store_for=media_store_for, send_reply=self._send_reply
        )
        self._test_hooks = _test_hooks

        # Observability only: session_key → handoff tasks still in flight.
        # Nothing gates on this; turns are serialised by the agent.
        self._in_flight: dict[str, int] = {}

        # Strong references to spawned tasks — prevents GC before completion.
        self._pending_tasks: set[asyncio.Task[None]] = set()

        # Outbound channel registry — (platform, agent_did) → adapter. Keying by
        # platform ALONE collides when several bots share a platform (one
        # Telegram bot per agent): the last registered would capture every
        # reply, so a message to Olivia's bot would answer through Sales'. The
        # agent_did in the key routes each reply back through the bot it hit
        # (see _resolve_outbound). All send/edit/typing specifics live in the
        # adapter packages; this same registry also serves pairing DMs.
        self._adapters: dict[tuple[str, str], _AdapterProtocol] = {}
        if adapter is not None:
            self._adapters[_adapter_key(adapter)] = adapter
        if adapter_map:
            for entry in adapter_map.values():
                self._adapters[_adapter_key(entry)] = entry
        self._delivery_target_factory = delivery_target_factory
        self._stream_bridge = StreamBridge()

        # Composed pairing interceptor (T1.8) — shares the same channel registry.
        # Pairing keeps its own platform-name→adapter map (name-keyed, seeded
        # here and topped up by register_adapter). Pairing DM routing across
        # multiple same-platform bots is a separate, lesser concern than reply
        # routing and is intentionally last-wins for now.
        self._pairing = PairingInterceptor(
            user_allowlist=user_allowlist,
            pairing_store=pairing_store,
            pairing_db_path=pairing_db_path,
            adapter_map={name: a for (name, _did), a in self._adapters.items()},
        )

        # Test instrumentation. Tests assert on per-session handoff counts;
        # production code must NOT gate logic on this.
        self.agent_tasks_spawned: dict[str, int] = {}

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

        Idempotent: re-registering the same (platform, agent) replaces it.
        """
        self._adapters[_adapter_key(adapter)] = adapter
        self._pairing.register_adapter(adapter.name, adapter)
        set_disconnect_handler = getattr(adapter, "set_disconnect_handler", None)
        if callable(set_disconnect_handler):
            set_disconnect_handler(self.cancel_web_session)

    def set_adapter(self, adapter: BasePlatformAdapter) -> None:
        """Backwards-compatible alias for :meth:`register_adapter`."""
        self.register_adapter(adapter)

    async def cancel_web_session(self, _chat_id: str, agent_did: str, user_did: str) -> None:
        """Cancel a browser-only live run after its final socket disconnects."""
        cancel_session = getattr(self._executor, "cancel_session", None)
        if not callable(cancel_session):
            return
        session_key = self.current_session_key(agent_did, user_did)
        await cancel_session(agent_did, session_key)

    async def send(self, target: DeliveryTarget, message: str, *, agent_did: str = "") -> None:
        """Deliver an unsolicited outbound message to ``target``'s platform.

        The outbound path for agent-initiated delivery (fired schedules,
        proactive notifications) that does not originate from an inbound turn.
        ``agent_did`` selects the sending bot when several serve the platform
        (one Telegram bot per agent) — pass the delivering agent's DID so its
        schedule/notification goes out through ITS bot, not another agent's.
        No-op with a structured warning when no adapter serves the pair, so a
        stale ``deliver_to`` never raises into the caller (delivery is fail-open).
        """
        adapter = self._adapter_for(target.platform, agent_did)
        if adapter is None:
            _logger.warning(
                "Outbound send dropped: no adapter for platform %r agent %r (known: %s)",
                target.platform,
                agent_did,
                ", ".join(sorted(f"{p}/{a}" for p, a in self._adapters)) or "none",
            )
            return
        await adapter.send(target, message)

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

    def _canonicalise(self, event: InboundEvent) -> InboundEvent:
        """Resolve the event's user DID and stamp the pair's CURRENT session key.

        Every entry point routes through here, so a surface that supplies a
        stale key — or none at all — still lands on the pair's current session,
        including after a ``/new``. Two entry points resolving identity their
        own way is how a turn ends up in the conversation the operator just
        cleared. Synchronous (SQLite read) so ``handle`` can call it before its
        race guard without an intervening await.
        """
        resolved_did = event.user_did
        if self._identity_graph is not None:
            resolved_did = self._resolve_user_did(event.platform, event.user_did)
        canonical_key = self.current_session_key(event.agent_did, resolved_did)
        if resolved_did == event.user_did and event.session_key == canonical_key:
            return event
        return event.model_copy(update={"user_did": resolved_did, "session_key": canonical_key})

    # -----------------------------------------------------------------------
    # Core routing
    # -----------------------------------------------------------------------

    async def handle(self, event: InboundEvent | InboundDraft) -> None:
        """Route an inbound message to its session.

        The primary entry point for platform adapters. An adapter hands up an
        :class:`~arcgateway.adapters.base.InboundDraft` — platform identity plus
        parts, with artefacts still on the platform — and the gateway takes
        custody of those artefacts here. Surfaces with nothing to fetch (web,
        in-process, programmatic callers) pass a finished ``InboundEvent``.

        Pairing intercept runs BEFORE any routing. If the user is not in the
        allowlist, the message is intercepted (code minted and DM'd) and this
        method returns WITHOUT routing to the agent.

        Custody runs AFTER pairing, deliberately: downloading and writing an
        unpaired sender's file into the agent's workspace would let anyone who
        can find the bot put bytes on the operator's disk without ever being
        approved.

        Every surviving event gets its own handoff task and is delivered to the
        agent — none is held back. Two messages arriving in the same event-loop
        tick therefore both reach the agent, which serialises them into one turn
        (see module docstring).

        Args:
            event: A draft from a platform adapter, or a finished inbound event.
        """
        # One narrowing, once: everything below this point works on a finished
        # InboundEvent, and ``draft`` is only re-consulted to take custody of the
        # artefacts it still names.
        draft: InboundDraft | None = None
        inbound: InboundEvent
        if isinstance(event, InboundDraft):
            draft, inbound = event, _draft_to_event(event)
        else:
            inbound = event

        # --- Identity + canonical session key (T1.3 / SDD §3.3, D-06) ---
        # The gateway core owns session-key policy: a filename-safe,
        # cross-platform-stable key derived from (agent, user). Adapters supply
        # platform identity only — they must NOT hand-craft the session key.
        # A raw "{agent_did}:{platform}:{chat_type}:{user}" string breaks the
        # executor's filename-safe key validator whenever the agent DID
        # contains '/' or ':' (e.g. did:arc:local:executor/abc). Deriving the
        # canonical key here makes every platform consistent and safe.
        # Synchronous (SQLite read) — no await before the race guard.
        inbound = self._canonicalise(inbound)

        # --- Pairing interceptor (T1.8) ---
        if not await self._pairing.is_user_approved(inbound.user_did, inbound.platform):
            await self._pairing.handle_unpaired_user(inbound)
            return

        # --- Slash-command interceptor ---
        # Registered commands (e.g. /new) are handled here and never reach the
        # session/executor machinery; an unknown "/token" falls through as
        # ordinary text. Runs AFTER pairing so an unapproved user cannot rotate
        # sessions or enumerate commands.
        async def _reply(text: str) -> None:
            await self._send_reply(inbound, text)

        if await self._commands.dispatch(
            inbound, inbound.agent_did, inbound.user_did, self, _reply
        ):
            return

        # --- Media custody (SPEC-065 REQ-297/298/299) ---
        # The artefacts are fetched and written here, not by the adapter, so
        # there is one implementation of the path, the ceiling and the audit
        # event however many platforms the gateway grows.
        if draft is not None:
            taken = await self._custodian.take(draft, inbound)
            if taken is None:
                return
            inbound = taken

        session_key = inbound.session_key

        # Bookkeeping is synchronous so a burst of messages is counted exactly
        # once each, whatever order their tasks run in.
        self._in_flight[session_key] = self._in_flight.get(session_key, 0) + 1
        if self._test_hooks:
            self.agent_tasks_spawned[session_key] = (
                self.agent_tasks_spawned.get(session_key, 0) + 1
            )

        task = asyncio.create_task(
            self._process_session(session_key, inbound),
            name=f"session:{session_key}",
        )
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    # -----------------------------------------------------------------------
    # Private session processing
    # -----------------------------------------------------------------------

    async def _process_session(self, session_key: str, event: InboundEvent) -> None:
        """Hand one event to the agent and forward whatever comes back.

        Runs as an asyncio.Task. Fail-open: an error here must not kill the
        adapter's receive loop, so it is logged and the task ends.

        Args:
            session_key: Session the event belongs to.
            event: The event to deliver.
        """
        try:
            await self._run_turn(session_key, event)
        except Exception:  # reason: fail-open — log + continue
            _logger.exception("Unhandled error in session %s turn", session_key)
        finally:
            remaining = self._in_flight.get(session_key, 1) - 1
            if remaining > 0:
                self._in_flight[session_key] = remaining
            else:
                self._in_flight.pop(session_key, None)

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
                dispatch_delta = getattr(adapter, "dispatch_delta", None)
                if callable(dispatch_delta):
                    async for delta in delta_stream:
                        await dispatch_delta(target, delta)
                        if delta.is_final:
                            return
                else:
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

        Per-session serialisation is the agent's, exactly as for ``handle()``:
        two concurrent calls for one session_key both reach the delivery entry
        point, and the second joins the turn the first opened rather than
        starting a second one. Its own delta stream then ends without content,
        because the reply to both messages rides the first stream.

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
        # Identity + canonical session key (same step as handle()).
        event = self._canonicalise(event)

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

        With several bots on one platform (one per agent), the reply must go
        through the bot the message HIT — resolved by (platform, agent_did).
        Falls back to the platform's sole bot, then to the sole channel overall
        (single-platform deployments and tests). When several match none, there
        is no safe channel — the caller logs the turn instead of guessing.
        """
        return self._adapter_for(event.platform, event.agent_did)

    def _adapter_for(self, platform: str, agent_did: str) -> _AdapterProtocol | None:
        """Resolve the outbound adapter for a (platform, agent) pair, with
        fallbacks for single-bot and single-channel deployments."""
        adapter = self._adapters.get((platform, agent_did or ""))
        if adapter is not None:
            return adapter
        on_platform = [a for (p, _), a in self._adapters.items() if p == platform]
        if len(on_platform) == 1:
            return on_platform[0]
        if len(self._adapters) == 1:
            return next(iter(self._adapters.values()))
        return None

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
        """Return the number of sessions with a handoff still in flight."""
        return len(self._in_flight)
