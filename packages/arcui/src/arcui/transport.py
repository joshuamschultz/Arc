"""UITransport protocol and InMemoryTransport for testing.

UITransport abstracts the communication channel between agents and the UI
server. InMemoryTransport uses asyncio.Queue pairs for in-process testing
without network.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol, runtime_checkable

from arcui.types import ControlMessage, ControlResponse, UIEvent


@runtime_checkable
class UITransport(Protocol):
    """Abstract transport for agent <-> UI communication."""

    async def send_event(self, agent_id: str, event: UIEvent | ControlResponse) -> None:
        """Send a UIEvent or ControlResponse from agent to server."""
        ...

    async def send_control(self, agent_id: str, message: ControlMessage) -> None:
        """Send a ControlMessage from server to agent."""
        ...

    async def receive(
        self,
    ) -> tuple[str, UIEvent | ControlMessage | ControlResponse]:
        """Receive next message. Blocks until available."""
        ...

    async def close(self) -> None:
        """Close the transport."""
        ...


class InMemoryTransport:
    """Queue-based UITransport for testing without network.

    Created in pairs via ``create_pair()``. One side sends events, the other
    receives them — and vice versa for control messages.
    """

    def __init__(
        self,
        outbox: asyncio.Queue[tuple[str, Any]],
        inbox: asyncio.Queue[tuple[str, Any]],
    ) -> None:
        self._outbox = outbox
        self._inbox = inbox
        self._closed = False

    @classmethod
    def create_pair(cls) -> tuple[InMemoryTransport, InMemoryTransport]:
        """Create a linked pair of transports.

        Returns (client, server). Client's outbox is server's inbox and
        vice versa.
        """
        a_to_b: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        b_to_a: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        client = cls(outbox=a_to_b, inbox=b_to_a)
        server = cls(outbox=b_to_a, inbox=a_to_b)
        return client, server

    async def send_event(self, agent_id: str, event: UIEvent | ControlResponse) -> None:
        if self._closed:
            raise RuntimeError("Transport is closed")
        await self._outbox.put((agent_id, event))

    async def send_control(self, agent_id: str, message: ControlMessage) -> None:
        if self._closed:
            raise RuntimeError("Transport is closed")
        await self._outbox.put((agent_id, message))

    async def receive(
        self,
    ) -> tuple[str, UIEvent | ControlMessage | ControlResponse]:
        if self._closed:
            raise RuntimeError("Transport is closed")
        return await self._inbox.get()

    async def close(self) -> None:
        self._closed = True
