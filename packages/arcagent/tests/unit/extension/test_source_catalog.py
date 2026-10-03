"""The catalog must never lose a connection while it is being replaced."""

from __future__ import annotations

import asyncio

import pytest

from arcagent.extension.source_catalog import SourceCatalog


class _Adapter:
    """A source adapter that only has to exist and close."""

    def __init__(self) -> None:
        self.closed = False

    async def close_source(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_a_connection_is_never_absent_while_being_replaced() -> None:
    """Reconcile re-registers every connection, and it must not blink out.

    Detaching before installing the replacement left a window with no entry at
    all: the listing lost the source, and an operator who clicked in that window
    was told it was not found.
    """
    catalog = SourceCatalog()
    await catalog.register("dropbox", _Adapter())

    seen: list[int] = []

    async def watch() -> None:
        for _ in range(80):
            seen.append(len(await catalog.snapshot()))
            await asyncio.sleep(0)

    watcher = asyncio.create_task(watch())
    for _ in range(20):
        await catalog.register("dropbox", _Adapter())
    await watcher

    assert seen
    assert min(seen) == 1


@pytest.mark.asyncio
async def test_replacing_an_adapter_closes_the_one_it_replaced() -> None:
    catalog = SourceCatalog()
    first = _Adapter()
    await catalog.register("dropbox", first)

    await catalog.register("dropbox", _Adapter())

    assert first.closed


@pytest.mark.asyncio
async def test_registering_the_same_adapter_again_is_a_no_op() -> None:
    """Reconcile is idempotent, so an unchanged adapter must not be torn down."""
    catalog = SourceCatalog()
    adapter = _Adapter()
    await catalog.register("dropbox", adapter)

    await catalog.register("dropbox", adapter)

    assert not adapter.closed
    assert len(await catalog.snapshot()) == 1


@pytest.mark.asyncio
async def test_unregister_removes_and_closes() -> None:
    catalog = SourceCatalog()
    adapter = _Adapter()
    await catalog.register("dropbox", adapter)

    await catalog.unregister("dropbox")

    assert adapter.closed
    assert await catalog.snapshot() == ()


async def _hold_lease(catalog: SourceCatalog, connection_id: str, leased: asyncio.Event) -> str:
    """A sync run: holds the lease until cancelled, then releases it cleanly."""
    try:
        async with catalog.lease(connection_id, cancel_on_retire=True):
            leased.set()
            await asyncio.Event().wait()  # a six-hour sync
    except asyncio.CancelledError:
        return "cancelled"
    return "finished"


@pytest.mark.asyncio
async def test_replacing_never_waits_for_a_running_sync() -> None:
    """A grant change must not block behind the longest sync on the agent."""
    catalog = SourceCatalog()
    old = _Adapter()
    await catalog.register("dropbox", old)
    leased = asyncio.Event()
    sync = asyncio.create_task(_hold_lease(catalog, "dropbox", leased))
    await leased.wait()

    new = _Adapter()
    async with asyncio.timeout(1):
        await catalog.register("dropbox", new)

    [registration] = await catalog.snapshot()
    assert registration.adapter is new
    # The old adapter's run is cancelled cooperatively, then the adapter closes.
    async with asyncio.timeout(1):
        assert await sync == "cancelled"
        await catalog.drain_retired()
    assert old.closed


@pytest.mark.asyncio
async def test_removing_never_waits_for_a_running_sync() -> None:
    """Operator revocation must not wait out a stuck sync."""
    catalog = SourceCatalog()
    adapter = _Adapter()
    await catalog.register("dropbox", adapter)
    leased = asyncio.Event()
    sync = asyncio.create_task(_hold_lease(catalog, "dropbox", leased))
    await leased.wait()

    async with asyncio.timeout(1):
        await catalog.unregister("dropbox")

    assert await catalog.snapshot() == ()
    async with asyncio.timeout(1):
        assert await sync == "cancelled"
        await catalog.drain_retired()
    assert adapter.closed


@pytest.mark.asyncio
async def test_close_waits_for_retired_adapters_to_close() -> None:
    catalog = SourceCatalog()
    adapter = _Adapter()
    await catalog.register("dropbox", adapter)
    leased = asyncio.Event()
    sync = asyncio.create_task(_hold_lease(catalog, "dropbox", leased))
    await leased.wait()

    async with asyncio.timeout(1):
        await catalog.close()

    assert adapter.closed
    assert sync.done()
