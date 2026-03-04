"""EventBuffer — bounded deque with periodic flush to ConnectionManager.

Batches incoming events and flushes to WebSocket clients every 100ms.
Supports both raw dicts (legacy) and UIEvent objects (multi-agent).
When a SubscriptionManager is provided, UIEvents are broadcast with
server-side filtering instead of going through ConnectionManager.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import TYPE_CHECKING, Any

from arcui.connection import ConnectionManager
from arcui.types import UIEvent

if TYPE_CHECKING:
    from arcui.subscription import SubscriptionManager

logger = logging.getLogger(__name__)


class EventBuffer:
    """Bounded event buffer with periodic flush loop.

    Events are pushed via push(), accumulated in a deque, and flushed
    every flush_interval_ms. UIEvent objects go through SubscriptionManager
    for filtered delivery; raw dicts use ConnectionManager.broadcast.
    """

    def __init__(
        self,
        connection_manager: ConnectionManager,
        *,
        subscription_manager: SubscriptionManager | None = None,
        maxlen: int = 1000,
        flush_interval_ms: int = 100,
    ) -> None:
        self._cm = connection_manager
        self._sub_mgr = subscription_manager
        self._buffer: deque[dict[str, Any] | UIEvent] = deque(maxlen=maxlen)
        self._flush_interval = flush_interval_ms / 1000.0
        self._task: asyncio.Task[None] | None = None

    def push(self, data: dict[str, Any] | UIEvent) -> None:
        """Add an event to the buffer. Accepts raw dicts or UIEvent."""
        self._buffer.append(data)

    def flush_once(self) -> None:
        """Flush buffer contents once (synchronous). Used by tests and one-shot scenarios."""
        if not self._buffer:
            return

        has_sub_mgr = self._sub_mgr is not None
        has_clients = self._cm.client_count > 0

        if not has_sub_mgr and not has_clients:
            return

        batch = list(self._buffer)
        self._buffer.clear()

        ui_events: list[UIEvent] = []
        raw_events: list[dict[str, Any]] = []
        for item in batch:
            if isinstance(item, UIEvent):
                ui_events.append(item)
            else:
                raw_events.append(item)

        if ui_events and has_sub_mgr:
            for event in ui_events:
                self._sub_mgr.broadcast_filtered(event)
        elif ui_events and has_clients:
            for event in ui_events:
                self._cm.broadcast(event.model_dump())

        if raw_events and has_clients:
            self._cm.broadcast({"type": "event_batch", "events": raw_events})

    async def _flush_loop(self) -> None:
        """Periodically flush buffer contents."""
        while True:
            await asyncio.sleep(self._flush_interval)
            self.flush_once()

    def start(self) -> None:
        """Start the background flush loop."""
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._flush_loop())

    def stop(self) -> None:
        """Stop the background flush loop."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            self._task = None

    @property
    def pending_count(self) -> int:
        """Number of events waiting to be flushed."""
        return len(self._buffer)
