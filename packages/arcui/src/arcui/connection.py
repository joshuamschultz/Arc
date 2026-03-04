"""ConnectionManager — per-client asyncio.Queue with broadcast.

Each WebSocket client gets a bounded queue. broadcast() pushes to all
registered queues. Queue-full handling: drop oldest message.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from arcui.ws_helpers import safe_enqueue

logger = logging.getLogger(__name__)

# Default per-client queue depth. Balances memory per client (~8KB at 1000
# typical JSON messages) vs burst tolerance during high-throughput telemetry.
_DEFAULT_QUEUE_SIZE = 1000


class ConnectionManager:
    """Manages per-client message queues for WebSocket broadcast."""

    def __init__(self, maxsize: int = _DEFAULT_QUEUE_SIZE) -> None:
        self._clients: set[asyncio.Queue[str]] = set()
        self._maxsize = maxsize

    def create_queue(self) -> asyncio.Queue[str]:
        """Create and register a new client queue."""
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=self._maxsize)
        self._clients.add(queue)
        return queue

    def unregister(self, queue: asyncio.Queue[str]) -> None:
        """Remove a client queue from broadcast list."""
        self._clients.discard(queue)

    def broadcast(self, data: Any) -> None:
        """Push JSON-serialized data to all registered client queues.

        If a queue is full, drops the oldest message to make room.
        """
        message = json.dumps(data) if not isinstance(data, str) else data
        for queue in self._clients:
            safe_enqueue(queue, message)

    @property
    def client_count(self) -> int:
        """Number of connected clients."""
        return len(self._clients)
