"""FileChangeBridge — fan out :class:`FileChangeEvent`s to subscribed browser clients.

This module is the arcui side of the gateway-to-browser file-change pipeline
described in SDD §3 and §4.6:

::

    arcgateway.fs_watcher
       │  emits FileChangeEvent
       ▼
    arcgateway.file_events.FileEventBus
       │  fan out to listeners
       ▼
    arcui.file_change_bridge.FileChangeBridge   ← this module
       │  per-client filter (subscribed agent_ids)
       ▼
    asyncio.Queue → /ws → browser

Design notes
------------
* :class:`FileChangeEvent` carries an ``event_type`` like
  ``"policy:bullets_updated"`` — the colon disqualifies it from
  :class:`arcui.types.UIEvent`'s ``^[a-z_]+$`` pattern, so we ship a separate
  ``{"type": "file_change", ...}`` envelope.

* A bounded ring of recent events (default 100) is retained for reconnect
  replay (PLAN 3.4): on subscribe the client sees the latest known state for
  that agent immediately rather than waiting for the next disk write.

* The bridge holds **only** the (queue, agent_id) registry. WS lifecycle
  cleanup is the route handler's job; the bridge exposes
  :meth:`remove_all_for` to do it in O(1) per agent.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from arcgateway.file_events import FileChangeEvent

from arcui.ws_helpers import safe_enqueue

if TYPE_CHECKING:
    import asyncio

    from arcgateway.file_events import FileEventBus

logger = logging.getLogger(__name__)


_DEFAULT_REPLAY = 100


class FileChangeBridge:
    """Per-client fan-out for :class:`FileChangeEvent` with bounded replay."""

    def __init__(self, *, max_replay: int = _DEFAULT_REPLAY) -> None:
        self._subs: dict[str, set[asyncio.Queue[str]]] = {}
        self._ring: deque[FileChangeEvent] = deque(maxlen=max_replay)
        self._listener: Callable[[FileChangeEvent], Awaitable[None]] | None = None

    # ------------------------------------------------------------------
    # Subscription registry
    # ------------------------------------------------------------------

    def add_subscription(self, queue: asyncio.Queue[str], agent_id: str) -> None:
        self._subs.setdefault(agent_id, set()).add(queue)

    def remove_subscription(self, queue: asyncio.Queue[str], agent_id: str) -> None:
        bucket = self._subs.get(agent_id)
        if bucket is None:
            return
        bucket.discard(queue)
        if not bucket:
            self._subs.pop(agent_id, None)

    def remove_all_for(self, queue: asyncio.Queue[str]) -> set[str]:
        """Drop every subscription owned by ``queue``. Returns the agent_ids removed."""
        removed: set[str] = set()
        for agent_id, bucket in list(self._subs.items()):
            if queue in bucket:
                bucket.discard(queue)
                removed.add(agent_id)
                if not bucket:
                    self._subs.pop(agent_id, None)
        return removed

    def subscribers_for(self, agent_id: str) -> set[asyncio.Queue[str]]:
        return set(self._subs.get(agent_id, set()))

    # ------------------------------------------------------------------
    # Event handling + replay
    # ------------------------------------------------------------------

    async def handle_event(self, event: FileChangeEvent) -> None:
        """Fan an event out to subscribed clients and record it in the replay ring.

        Bus listener entrypoint. Never raises — :class:`FileEventBus` would
        swallow a raise anyway, but failing soft here keeps the AU-5 spirit
        (audit/UI side-channels never interrupt the source path).
        """
        self._ring.append(event)
        bucket = self._subs.get(event.agent_id)
        if not bucket:
            return
        message = self._serialize(event)
        for queue in list(bucket):
            safe_enqueue(queue, message)

    def replay_for(self, queue: asyncio.Queue[str], agent_id: str) -> int:
        """Push every cached event matching ``agent_id`` to ``queue``.

        Returns the number of events replayed — useful for tests / metrics.
        """
        count = 0
        for event in self._ring:
            if event.agent_id != agent_id:
                continue
            safe_enqueue(queue, self._serialize(event))
            count += 1
        return count

    @staticmethod
    def _serialize(event: FileChangeEvent) -> str:
        return json.dumps(
            {
                "type": "file_change",
                "agent_id": event.agent_id,
                "event_type": event.event_type,
                "path": event.path,
                "payload": event.payload,
            }
        )

    # ------------------------------------------------------------------
    # Bus attachment
    # ------------------------------------------------------------------

    def attach(self, bus: FileEventBus) -> None:
        """Register :meth:`handle_event` as a listener on ``bus``. Idempotent."""
        if self._listener is None:
            self._listener = self.handle_event
        bus.subscribe(self._listener)

    def detach(self, bus: FileEventBus) -> None:
        """Unregister from ``bus``. No-op if not attached."""
        if self._listener is not None:
            bus.unsubscribe(self._listener)
