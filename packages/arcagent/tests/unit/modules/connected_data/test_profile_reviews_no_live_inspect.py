"""Profile reviews are read without a live provider call, and survive a dead credential.

Regression (2026-10-04, DGX): ``GET /api/agents/{a}/knowledge/profile-reviews``
answered 500 every 30 s for any agent whose FIRST granted connection had no
stored credential. ``list_review_items`` built its ingest port by live-inspecting
that connection, so ``ExtensionError [CREDENTIAL_MISSING]`` escaped the route.
The review store belongs to the agent, not to any one provider: reading it must
not call a provider, and a connection that cannot be inspected must not hide the
reviews the others can reach.
"""

from __future__ import annotations

from typing import Any

from arcagent.connected_data import SyncLimits
from arcagent.core.errors import ExtensionError
from arcagent.extension.source import InspectSource, SourceDataShape, SourceDescription
from arcagent.extension.source_catalog import SourceCatalog
from arcagent.modules.connected_data.service import ConnectedDataService


class _Source:
    def __init__(self, kind: str, *, credential: bool = True) -> None:
        self.kind = kind
        self.credential = credential
        self.inspected = 0

    async def inspect_source(self, request: InspectSource) -> SourceDescription:
        self.inspected += 1
        if not self.credential:
            raise ExtensionError(
                code="CREDENTIAL_MISSING",
                message=f"{self.kind!r} has no stored credential; connect it",
            )
        return SourceDescription(
            connection_id=request.connection_id,
            source_kind=self.kind,
            account_id="account",
            data_shape=SourceDataShape.DOCUMENT,
            display_name=self.kind,
        )

    async def close_source(self) -> None:
        return None


class _Port:
    async def list_review_items(self, *, status: str | None, source_id: str | None) -> list[Any]:
        return [{"review_id": "fact-1", "status": status}]

    async def aclose(self) -> None:
        return None


async def _service(*sources: tuple[str, _Source]) -> ConnectedDataService:
    catalog = SourceCatalog()
    for connection_id, source in sources:
        await catalog.register(connection_id, source)
    return ConnectedDataService(
        catalog,
        agent_did="did:arc:agent",
        sync_store_opener=None,
        ingest_factory=lambda _description: _Port(),
        limits=SyncLimits(),
        global_concurrency=1,
    )


async def test_a_connection_without_a_credential_does_not_break_profile_reviews() -> None:
    confluence = _Source("confluence", credential=False)
    dropbox = _Source("dropbox")
    service = await _service(("confluence", confluence), ("dropbox", dropbox))

    items = await service.list_review_items(status="pending")

    assert items == ({"review_id": "fact-1", "status": "pending"},)


async def test_a_known_connection_is_used_without_calling_the_provider() -> None:
    confluence = _Source("confluence", credential=False)
    dropbox = _Source("dropbox")
    service = await _service(("confluence", confluence), ("dropbox", dropbox))
    service._descriptions["dropbox"] = await _Source("dropbox").inspect_source(
        InspectSource(connection_id="dropbox")
    )

    items = await service.list_review_items(status="pending")

    assert items
    assert confluence.inspected == 0
    assert dropbox.inspected == 0


async def test_no_reachable_connection_reads_as_no_reviews() -> None:
    service = await _service(("confluence", _Source("confluence", credential=False)))

    assert await service.list_review_items(status="pending") == ()
