"""The sync monitor sleeps while a due source is already running (it never spins).

A source asked for again while its run is going is due at once, and the run
holding it cannot start a second one. The monitor used to compute "due now",
wait zero seconds and tick again, thousands of times a second, for as long as
the run lasted: a busy loop on the event loop that serves chat, NATS and health.
A running source wakes the monitor itself when its run ends.
"""

from __future__ import annotations

import asyncio
from typing import Any

from arcstore.source_sync import InMemorySourceSyncStore
from packages.arcagent.tests.modules.connected_data.test_connections_sweep_sync_loop import (
    _Provider,
    _service,
    _until,
)

from arcagent.extension.source import SyncSource, SyncSourcePage
from arcagent.extension.source_catalog import SourceCatalog


class _Gated(_Provider):
    """A source whose crawl waits until the test lets it go."""

    def __init__(self) -> None:
        super().__init__("ok")
        self.release = asyncio.Event()

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        self.synced.set()
        await self.release.wait()
        return await super().sync_source(request)


class _CountingCatalog(SourceCatalog):
    def __init__(self) -> None:
        super().__init__()
        self.ticks = 0

    async def snapshot(self) -> Any:
        self.ticks += 1
        return await super().snapshot()


async def test_a_source_asked_for_while_it_runs_does_not_spin_the_monitor() -> None:
    catalog = _CountingCatalog()
    provider = _Gated()
    await catalog.register("mail", provider)
    service = _service(catalog, InMemorySourceSyncStore())
    await service.start()
    try:
        await asyncio.wait_for(provider.synced.wait(), timeout=5)
        await service.sync_now("mail")  # due now, but its run is still going
        # Anything that wakes the monitor: here a second source being attached.
        await catalog.register("files", _Provider("ok"))
        await asyncio.sleep(0.05)
        before = catalog.ticks
        await asyncio.sleep(0.3)
        ticks = catalog.ticks - before
        provider.release.set()

        async def ran_again() -> bool:
            return provider.checkpoints.count(None) >= 1 and len(provider.checkpoints) >= 2

        assert await _until(ran_again), "the request made during the run was dropped"
    finally:
        provider.release.set()
        await service.close()
    assert ticks < 10, f"the monitor ticked {ticks} times in 0.3 s while waiting on a run"
