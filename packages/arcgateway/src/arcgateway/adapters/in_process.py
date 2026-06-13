"""In-process Python adapter — programmatic dispatch into the gateway.

Most gateway adapters speak a real chat platform's wire protocol
(Telegram, Slack, Mattermost, browser WebSocket). FastAPI hosts and
CLI demos that just want to drive a ``SessionRouter`` from their own
Python code had to either hand-roll an adapter or sidestep the router
entirely by calling ``AsyncioExecutor.run(event)`` directly — that
works for a single-visitor case but loses the router's per-session
serialization, so two concurrent messages to the same session race.

``PythonAdapter`` plugs that gap. Callers ``dispatch(event)`` to push
into the router and receive back a per-call ``DeltaStream`` that
iterates the executor's deltas in arrival order. The adapter satisfies
:class:`BasePlatformAdapter` so the router routes deltas back through
the normal ``send`` / ``send_with_id`` path; we simply intercept them
and put them on the dispatching call's queue.

Design notes:

- One adapter instance can service many concurrent dispatches; each
  ``dispatch()`` is keyed on the event's session_key so deltas land on
  the right stream when the router fans turns out to multiple sessions.
- The router serializes within a session_key, so two ``dispatch()``
  calls for the same session run sequentially. Calls for *different*
  session_keys interleave freely.
- ``DeltaStream`` is just an ``asyncio.Queue``-backed async iterator
  plus a timeout. The terminal ``Delta(kind="done", is_final=True)``
  closes the stream — same protocol the executor already speaks.
- The platform name is the literal ``"python"`` so audit events and
  delivery targets can be filtered by source the same way real
  adapters can.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from arcgateway.delivery import DeliveryTarget
from arcgateway.executor import Delta, InboundEvent

_logger = logging.getLogger("arcgateway.adapters.in_process")

_DEFAULT_MAX_INFLIGHT: int = 32


class DeltaStream:
    """Per-dispatch async iterator over executor Deltas.

    The terminal ``Delta(kind="done", is_final=True)`` ends iteration;
    subsequent ``__anext__`` calls raise ``StopAsyncIteration``. A
    ``timeout`` argument on ``dispatch()`` bounds the wait between
    deltas — exceeding it raises ``asyncio.TimeoutError``.
    """

    def __init__(
        self,
        queue: asyncio.Queue[Delta],
        *,
        session_key: str,
        timeout: float | None,
    ) -> None:
        self._queue = queue
        self._session_key = session_key
        self._timeout = timeout
        self._closed = False

    @property
    def session_key(self) -> str:
        return self._session_key

    def __aiter__(self) -> DeltaStream:
        return self

    async def __anext__(self) -> Delta:
        if self._closed:
            raise StopAsyncIteration
        if self._timeout is None:
            delta = await self._queue.get()
        else:
            delta = await asyncio.wait_for(self._queue.get(), timeout=self._timeout)
        if delta.is_final:
            self._closed = True
        return delta


class PythonAdapter:
    """In-process programmatic adapter.

    Construct one instance per process (the router holds a reference);
    each ``dispatch()`` returns its own ``DeltaStream``. Adapters are
    duck-typed — ``BasePlatformAdapter`` is a ``Protocol``, so this
    class doesn't need to inherit. ``connect()`` / ``disconnect()``
    are no-ops because there's no platform connection to manage.
    """

    name: str = "python"

    def __init__(self, *, max_inflight: int = _DEFAULT_MAX_INFLIGHT) -> None:
        self._max_inflight = max_inflight
        # session_key -> queue. Cleared on done-delta or on stream timeout
        # so a long-lived adapter doesn't accumulate sessions forever.
        self._streams: dict[str, asyncio.Queue[Delta]] = {}

    # -- BasePlatformAdapter Protocol -------------------------------------

    async def connect(self) -> None:
        """No-op — this adapter has no platform connection."""

    async def disconnect(self) -> None:
        """Drain pending streams with a synthetic done-delta so callers
        iterating an open stream don't hang."""
        for sk, queue in list(self._streams.items()):
            queue.put_nowait(Delta(kind="done", is_final=True, turn_id=sk))
        self._streams.clear()

    async def send(
        self,
        target: DeliveryTarget,
        message: str,
        *,
        reply_to: str | None = None,
    ) -> None:
        """Route the adapter's outbound text into the dispatching call's stream.

        Real adapters write to a wire protocol here; we put a ``token``
        delta on the per-session queue. The router calls this once
        per StreamBridge turn (final send) — token-level deltas land
        on the queue via the bridge's internal protocol, not this method.
        """
        queue = self._streams.get(target.chat_id)
        if queue is None:
            _logger.debug(
                "PythonAdapter.send: no stream for chat_id=%s (already closed?)",
                target.chat_id,
            )
            return
        queue.put_nowait(
            Delta(kind="token", content=message, is_final=False, turn_id=target.chat_id)
        )

    async def send_with_id(self, target: DeliveryTarget, message: str) -> str | None:
        await self.send(target, message)
        return None

    # -- Programmatic dispatch -------------------------------------------

    async def dispatch(
        self,
        event: InboundEvent,
        router: Any,
        *,
        timeout: float | None = 120.0,
    ) -> DeltaStream:
        """Push an event into the router; return the outbound DeltaStream.

        The stream yields deltas in arrival order and terminates on the
        executor's ``done`` delta or after ``timeout`` seconds between
        deltas. ``router`` is the ``SessionRouter`` instance — passed
        explicitly so the adapter has no hidden global state.

        ``event.chat_id`` doubles as the stream key; callers picking
        chat_ids that collide with another in-flight dispatch will see
        deltas interleave on the same queue.
        """
        queue: asyncio.Queue[Delta] = asyncio.Queue(maxsize=self._max_inflight)
        self._streams[event.chat_id] = queue
        try:
            await router.handle(event)
        except Exception:
            # Ensure the consumer sees a clean termination even when
            # routing fails before the executor produced a final delta.
            queue.put_nowait(Delta(kind="done", is_final=True, turn_id=event.chat_id))
            self._streams.pop(event.chat_id, None)
            raise
        return DeltaStream(queue, session_key=event.session_key, timeout=timeout)

    # -- Internal use by SessionRouter.dispatch_and_await ----------------

    def _put(self, chat_id: str, delta: Delta) -> None:
        """Push a delta directly onto a stream queue (test/router seam)."""
        queue = self._streams.get(chat_id)
        if queue is not None:
            queue.put_nowait(delta)

    def _stream_iterator(self, chat_id: str, *, timeout: float | None) -> AsyncIterator[Delta]:
        """Adopt an existing queue without sending an event through the router.

        Used by ``SessionRouter.dispatch_and_await`` to set up the
        delta stream before the routing call returns, so the caller
        gets back a stream that's already wired to receive deltas.
        """
        queue = self._streams.get(chat_id)
        if queue is None:
            queue = asyncio.Queue(maxsize=self._max_inflight)
            self._streams[chat_id] = queue
        return DeltaStream(queue, session_key=chat_id, timeout=timeout)
