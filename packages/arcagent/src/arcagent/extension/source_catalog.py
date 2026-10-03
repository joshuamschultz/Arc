"""Process-local catalog for the optional connected-data source seam.

The connector module owns attachment lifetimes; the connected-data module owns
consumption.  This small catalog is the only bridge between them.  It carries
opaque source adapters and never stores credentials or raw source content.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

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
    #: Tasks holding a lease, so a retired adapter's runs can be cancelled.
    holders: set[asyncio.Task[object]] = field(default_factory=set)


class SourceCatalog:
    """Explicit, per-agent source registry with deterministic teardown.

    Replacing or removing a connection never waits for a sync that is using the
    old adapter. The old entry is retired: its lease holders are cancelled
    cooperatively (a sync marks itself cancelled and keeps its cursor), and the
    adapter is closed in the background once its last lease is released. A
    six-hour sync therefore cannot block an operator's grant change or removal.

    Consumers hear about every change through :meth:`on_change`. A synchronizer
    that started before the connectors attached their sources otherwise slept a
    whole interval on an empty catalog (connections sweep D1).
    """

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        self._condition = asyncio.Condition()
        self._retiring: set[asyncio.Task[None]] = set()
        self._listeners: list[Callable[[], None]] = []

    def on_change(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Call ``listener`` after every register, replace or unregister.

        The listener runs synchronously and must only signal (set an event); it
        is never told which source changed, so it re-reads :meth:`snapshot`.
        Returns the function that removes the listener again.
        """
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    def _changed(self) -> None:
        for listener in tuple(self._listeners):
            try:
                listener()
            except Exception:  # reason: one consumer's bug must not fail a registration
                _logger.warning("source catalog listener failed", exc_info=True)

    async def register(self, connection_id: str, adapter: SourceAdapter) -> None:
        """Register or replace a connection and retire the previous adapter.

        The replacement is installed before the old one is torn down, so the
        connection is never absent. Detaching first left a window in which the
        source did not exist at all: a reconcile — which runs on grant changes
        and at startup — made every connection blink out of the listing, and an
        operator who clicked in that window was told the source was not found.
        """
        registration = SourceRegistration(connection_id=connection_id, adapter=adapter)
        async with self._condition:
            previous = self._entries.get(connection_id)
            if previous is not None and previous.registration.adapter is adapter:
                return
            self._entries[connection_id] = _Entry(registration)
        self._changed()
        if previous is not None:
            await self._retire(previous)

    async def unregister(self, connection_id: str) -> None:
        """Remove one connection, if it is attached, and retire its adapter."""
        async with self._condition:
            previous = self._entries.pop(connection_id, None)
        if previous is not None:
            self._changed()
            await self._retire(previous)

    async def snapshot(self) -> tuple[SourceRegistration, ...]:
        """Return a stable snapshot for a synchronizer iteration."""
        async with self._condition:
            return tuple(entry.registration for entry in self._entries.values())

    @contextlib.asynccontextmanager
    async def lease(
        self, connection_id: str, *, cancel_on_retire: bool = False
    ) -> AsyncIterator[SourceRegistration | None]:
        """Hold an adapter alive while one sync operation uses it.

        With ``cancel_on_retire`` the holding task is cancelled when the adapter
        is replaced or removed, so a long run never delays the operator's change;
        the run must treat cancellation as a clean stop (keep its cursor).
        """
        task = asyncio.current_task() if cancel_on_retire else None
        async with self._condition:
            entry = self._entries.get(connection_id)
            if entry is None:
                yield None
                return
            entry.leases += 1
            if task is not None:
                entry.holders.add(task)
            registration = entry.registration
        try:
            yield registration
        finally:
            async with self._condition:
                entry.leases -= 1
                if task is not None:
                    entry.holders.discard(task)
                if entry.leases == 0:
                    self._condition.notify_all()

    async def drain_retired(self) -> None:
        """Wait until every retired adapter has been closed."""
        while self._retiring:
            pending = tuple(self._retiring)
            await asyncio.gather(*pending, return_exceptions=True)
            # A finished task's done-callback has not run yet; awaiting an
            # already-finished gather never yields, so drop them here.
            self._retiring.difference_update(pending)

    async def close(self) -> None:
        """Retire every adapter, then wait for all of them to close."""
        async with self._condition:
            connection_ids = tuple(self._entries)
        for connection_id in connection_ids:
            await self.unregister(connection_id)
        await self.drain_retired()

    async def _retire(self, entry: _Entry) -> None:
        """Close ``entry``'s adapter now if idle, else cancel its runs and close later."""
        async with self._condition:
            holders = tuple(entry.holders)
            busy = entry.leases > 0
        if not busy:
            await _close(entry.registration)
            return
        for holder in holders:
            holder.cancel()
        closer = asyncio.create_task(
            self._close_when_released(entry),
            name=f"source_catalog:retire:{entry.registration.connection_id}",
        )
        self._retiring.add(closer)
        closer.add_done_callback(self._retiring.discard)

    async def _close_when_released(self, entry: _Entry) -> None:
        async with self._condition:
            while entry.leases:
                await self._condition.wait()
        await _close(entry.registration)


async def _close(registration: SourceRegistration) -> None:
    try:
        await registration.adapter.close_source()
    except Exception:
        _logger.warning(
            "source adapter close failed: %s", registration.connection_id, exc_info=True
        )


__all__ = ["SourceCatalog", "SourceRegistration"]
