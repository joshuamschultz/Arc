"""EventBuffer — bounded deque with periodic flush to ConnectionManager.

Batches incoming events and flushes to WebSocket clients every 100ms.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any

from arcui.connection import ConnectionManager

logger = logging.getLogger(__name__)


class EventBuffer:
    """Bounded event buffer with periodic flush loop.

    Events are pushed via push(), accumulated in a deque, and flushed
    every flush_interval_ms to the ConnectionManager as a JSON batch.
    """

    def __init__(
        self,
        connection_manager: ConnectionManager,
        *,
        maxlen: int = 1000,
        flush_interval_ms: int = 100,
    ) -> None:
        self._cm = connection_manager
        self._buffer: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._flush_interval = flush_interval_ms / 1000.0
        self._task: asyncio.Task[None] | None = None

    def push(self, data: dict[str, Any]) -> None:
        """Add an event to the buffer."""
        self._buffer.append(data)

    async def _flush_loop(self) -> None:
        """Periodically flush buffer contents to ConnectionManager."""
        while True:
            await asyncio.sleep(self._flush_interval)
            if self._buffer and self._cm.client_count > 0:
                batch = list(self._buffer)
                self._buffer.clear()
                self._cm.broadcast({"type": "event_batch", "events": batch})

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
