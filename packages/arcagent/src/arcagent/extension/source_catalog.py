"""Process-local catalog for the optional connected-data source seam.

The connector module owns attachment lifetimes; the connected-data module owns
consumption.  This small catalog is the only bridge between them.  It carries
opaque source adapters and never stores credentials or raw source content.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

from arcagent.extension.source import SourceAdapter

_logger = logging.getLogger("arcagent.extension.source_catalog")


@dataclass(frozen=True)
class SourceRegistration:
    """One granted connection currently attached in this process."""

    connection_id: str
    adapter: SourceAdapter


@dataclass
class _Entry:
    registration: SourceRegistration
    leases: int = 0


class SourceCatalog:
    """Explicit, per-agent source registry with deterministic teardown."""

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        self._condition = asyncio.Condition()

    async def register(self, connection_id: str, adapter: SourceAdapter) -> None:
        """Register or replace a connection and close the previous adapter."""
        registration = SourceRegistration(connection_id=connection_id, adapter=adapter)
        previous = await self._detach(connection_id)
        async with self._condition:
            self._entries[connection_id] = _Entry(registration)
        if previous is not None and previous.adapter is not adapter:
            await _close(previous)

    async def unregister(self, connection_id: str) -> None:
        """Remove and close one connection, if it is attached."""
        previous = await self._detach(connection_id)
        if previous is not None:
            await _close(previous)

    async def snapshot(self) -> tuple[SourceRegistration, ...]:
        """Return a stable snapshot for a synchronizer iteration."""
        async with self._condition:
            return tuple(entry.registration for entry in self._entries.values())

    @contextlib.asynccontextmanager
    async def lease(self, connection_id: str) -> AsyncIterator[SourceRegistration | None]:
        """Hold an adapter alive while one sync operation uses it."""
        async with self._condition:
            entry = self._entries.get(connection_id)
            if entry is None:
                yield None
                return
            entry.leases += 1
            registration = entry.registration
        try:
            yield registration
        finally:
            async with self._condition:
                entry.leases -= 1
                if entry.leases == 0:
                    self._condition.notify_all()

    async def close(self) -> None:
        """Close every adapter and empty the catalog."""
        async with self._condition:
            connection_ids = tuple(self._entries)
        for connection_id in connection_ids:
            await self.unregister(connection_id)

    async def _detach(self, connection_id: str) -> SourceRegistration | None:
        async with self._condition:
            entry = self._entries.pop(connection_id, None)
            if entry is None:
                return None
            while entry.leases:
                await self._condition.wait()
        return entry.registration


async def _close(registration: SourceRegistration) -> None:
    try:
        await registration.adapter.close_source()
    except Exception:
        _logger.warning(
            "source adapter close failed: %s", registration.connection_id, exc_info=True
        )


__all__ = ["SourceCatalog", "SourceRegistration"]
