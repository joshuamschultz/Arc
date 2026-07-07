"""Read-only team-flow stream — the browser-facing half of SPEC-031 C10.

arcui is a **thin view**: it renders arcteam flows and forwards human input.
It never routes, signs, coordinates, or runs a loop. This module holds the
view-only primitives that make the team stream live:

* :class:`TeamStreamHub` — a per-socket fan-out, scoped by channel. This is
  the channel/team-scoped generalisation of the gateway's per-``chat_id``
  fan-out (``arcgateway.adapters.web``): same drop-oldest backpressure shape,
  replicated here rather than cross-imported so the view owns no gateway
  internals.
* :func:`render_team_frame` / :func:`default_handle_of` — turn a raw arcteam
  ``Message`` into a display frame that shows **handles, never DIDs**, and
  surfaces ``@mentions`` as a distinct list (REQ-062).
* :class:`TeamBusObserver` — a read-only subscriber that tails the one arcteam
  bus (through the injected messenger's public read API) and publishes rendered
  frames to the hub. It consumes; it never sends.

Everything here is fan-out + rendering. No signing, no routing, no policy — the
moment any of those appear, this stopped being a view.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable
from typing import Any, Protocol

logger = logging.getLogger(__name__)

_DEFAULT_QUEUE_MAXSIZE = 100
_DEFAULT_RING_MAXLEN = 200
_DEFAULT_POLL_INTERVAL_SECONDS = 1.0
_OBSERVER_PAGE_LIMIT = 500


def default_handle_of(ref: str) -> str:
    """Map any address ref to a display handle — **never** a raw DID.

    Handles arrive in three shapes on the bus: a ``did:`` string, a
    ``scheme://handle`` URI, or a bare ``@handle``. All collapse to the trailing
    handle segment so the UI shows ``intake``, not ``did:arc:local:agent/intake``
    (REQ-062).
    """
    if ref.startswith("did:"):
        tail = ref.rsplit("/", 1)[-1]
        return tail.rsplit(":", 1)[-1]
    if "://" in ref:
        return ref.split("://", 1)[1]
    return ref.lstrip("@")


def _channel_of(to: list[str]) -> str:
    """Return the first ``channel://`` target name in ``to``, or ``""``."""
    for target in to:
        if target.startswith("channel://"):
            return target.split("://", 1)[1]
    return ""


def render_team_frame(
    message: dict[str, Any],
    handle_of: Callable[[str], str] = default_handle_of,
) -> dict[str, Any]:
    """Render a raw arcteam ``Message`` dict into a browser display frame.

    The frame is deliberately flat and handle-only: ``from`` and every entry of
    ``mentions`` pass through ``handle_of`` so a DID can never reach the browser.
    ``mentions`` is a distinct list so the UI can highlight them (REQ-062).
    """
    return {
        "type": "team_message",
        "channel": _channel_of(message.get("to", [])),
        "from": handle_of(str(message.get("sender", ""))),
        "body": str(message.get("body", "")),
        "mentions": [handle_of(str(m)) for m in message.get("mentions", [])],
        "action_required": bool(message.get("action_required", False)),
        "priority": str(message.get("priority", "normal")),
        "seq": int(message.get("seq", 0)),
        "ts": str(message.get("ts", "")),
        "id": str(message.get("id", "")),
    }


class TeamStreamHub:
    """Fan out rendered team frames to subscribed browser sockets, by channel.

    A socket registered with ``channels=None`` sees every channel (the team-wide
    observer view); a socket registered with an explicit set sees only those.
    Backpressure is drop-oldest per socket — a slow browser never blocks the
    stream or another viewer.

    A bounded ring of recently-published frames is replayed to each newly
    registered socket so a late-joining viewer immediately sees recent flow
    rather than an empty panel until the next message arrives. Registration is
    synchronous (no ``await``), so the ring snapshot and the socket's entry in
    the fan-out map are installed atomically — no missed or duplicated frame.
    """

    def __init__(
        self,
        *,
        queue_maxsize: int = _DEFAULT_QUEUE_MAXSIZE,
        ring_maxlen: int = _DEFAULT_RING_MAXLEN,
    ) -> None:
        self._queue_maxsize = queue_maxsize
        self._queues: dict[Any, asyncio.Queue[dict[str, Any]]] = {}
        self._scopes: dict[Any, set[str] | None] = {}
        self._ring: deque[dict[str, Any]] = deque(maxlen=ring_maxlen)

    def register(self, ws: Any, *, channels: set[str] | None = None) -> None:
        """Subscribe ``ws`` to the stream, replaying recent in-scope frames."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_maxsize)
        self._queues[ws] = queue
        self._scopes[ws] = channels
        for frame in self._ring:
            if channels is not None and str(frame.get("channel", "")) not in channels:
                continue
            self._enqueue_drop_oldest(queue, frame)

    def unregister(self, ws: Any) -> None:
        """Drop ``ws`` from the stream. Idempotent."""
        self._queues.pop(ws, None)
        self._scopes.pop(ws, None)

    def queue_for(self, ws: Any) -> asyncio.Queue[dict[str, Any]]:
        """Return the frame queue the route drains for ``ws``."""
        return self._queues[ws]

    async def next_frame(self, ws: Any) -> dict[str, Any]:
        """Await the next frame queued for ``ws`` (used by the route drain)."""
        return await self._queues[ws].get()

    @staticmethod
    def _enqueue_drop_oldest(queue: asyncio.Queue[dict[str, Any]], frame: dict[str, Any]) -> str:
        """Enqueue ``frame``; on a full queue drop the oldest and retry.

        Returns the delivery outcome: ``delivered``, ``dropped_backpressure``,
        or ``dead`` (queue unusable).
        """
        try:
            queue.put_nowait(frame)
            return "delivered"
        except asyncio.QueueFull:
            try:
                _ = queue.get_nowait()
                queue.put_nowait(frame)
                return "dropped_backpressure"
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                return "dead"

    async def publish(self, frame: dict[str, Any]) -> dict[str, int]:
        """Fan ``frame`` out to every socket whose scope matches its channel.

        Returns a ``{delivered, dropped_backpressure, dead}`` breakdown so the
        caller (or a test) can observe delivery without reaching into queues.
        """
        self._ring.append(frame)
        channel = str(frame.get("channel", ""))
        breakdown = {"delivered": 0, "dropped_backpressure": 0, "dead": 0}
        for ws, queue in list(self._queues.items()):
            scope = self._scopes.get(ws)
            if scope is not None and channel not in scope:
                continue
            breakdown[self._enqueue_drop_oldest(queue, frame)] += 1
        return breakdown


class _MessengerReader(Protocol):
    """The read-only slice of arcteam's MessagingService the observer needs.

    Duck-typed on purpose: arcui depends on no arcteam types, so the messenger
    is injected and only its public read surface is touched — never ``send``.
    """

    async def list_channels(self) -> list[Any]: ...

    async def list_channel_messages(
        self, channel_name: str, after_seq: int, limit: int
    ) -> list[Any]: ...


class TeamBusObserver:
    """Tail the arcteam bus read-only and publish rendered frames to the hub.

    This is the single-bus read-only subscriber SPEC-031 allows (in contrast to
    the banned parallel push pipeline). It never sends, signs, or acks another
    consumer's cursor — it reads new channel messages and renders them.
    """

    def __init__(
        self,
        service: _MessengerReader,
        hub: TeamStreamHub,
        *,
        handle_of: Callable[[str], str] = default_handle_of,
    ) -> None:
        self._service = service
        self._hub = hub
        self._handle_of = handle_of
        self._last_seq: dict[str, int] = {}

    async def poll_once(self) -> int:
        """Publish every not-yet-seen channel message. Returns the count."""
        published = 0
        for channel in await self._service.list_channels():
            name = str(getattr(channel, "name", channel))
            after = self._last_seq.get(name, 0)
            messages = await self._service.list_channel_messages(name, after, _OBSERVER_PAGE_LIMIT)
            for message in messages:
                raw = message.model_dump() if hasattr(message, "model_dump") else dict(message)
                await self._hub.publish(render_team_frame(raw, self._handle_of))
                self._last_seq[name] = int(raw.get("seq", self._last_seq.get(name, 0)))
                published += 1
        return published

    async def run(self, *, interval: float = _DEFAULT_POLL_INTERVAL_SECONDS) -> None:
        """Poll the bus forever at ``interval``. Fail-open on transient errors."""
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # reason: fail-open — a bus hiccup must not kill the view
                logger.exception("TeamBusObserver: poll failed; retrying")
            await asyncio.sleep(interval)
