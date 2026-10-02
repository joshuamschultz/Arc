"""One audit summary per committed page, not one row per object fetched (P18-1, 6.3).

DGX evidence: 26.8k ``connector.source.fetch`` lines per agent in three days, burying
the rows that matter. The per-read GRANT check is a security property and stays; what
collapses is the routine success. A refused or failed read, a skipped object and a
failed object still audit on their own.
"""

from __future__ import annotations

from typing import Any

from arcstore.source_sync import InMemorySourceSyncStore

from arcagent.connected_data import KnowledgeHome, MappingPlan, SyncError, SyncLimits, SyncStatus
from arcagent.extension.source import (
    FetchSourceObject,
    SourceContent,
    SourceDescription,
    SourceError,
    SourceFailureCode,
    SourceObject,
    SourceObjectKind,
    SyncSource,
    SyncSourcePage,
)
from arcagent.modules.connected_data import ConnectedDataCoordinator
from arcagent.modules.connectors.source_authorization import SourceAuthorizationBinding

_DID = "did:agent"


def _object(object_id: str) -> SourceObject:
    return SourceObject(
        object_id=object_id, locator=object_id, kind=SourceObjectKind.FILE, version="1", size=1
    )


class _Source:
    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        return SyncSourcePage(
            objects=tuple(_object(name) for name in ("a", "b", "c", "d")),
            next_checkpoint="end",
            has_more=False,
        )

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        return SourceContent(
            object_id=request.object_id,
            version=request.version,
            media_type="text/plain",
            content=b"x",
        )

    async def close_source(self) -> None:
        return None


class _Ingest:
    """Ingests every object except ``c``, which fails in a way charged to that object."""

    async def require_approved_mapping(self, source: SourceDescription) -> MappingPlan:
        return MappingPlan(
            mapping_id="m", homes=(KnowledgeHome.DOCUMENT,), revision="r", content_hash="h"
        )

    async def ingest(self, source: Any, source_object: Any, content: Any, mapping: Any) -> None:
        if source_object.object_id == "c":
            raise SyncError("this one object is unreadable", code="ingest_error")

    async def complete_snapshot(self, source: Any, object_ids: Any, mapping: Any) -> None:
        return None

    async def reset_source(self, source: SourceDescription) -> None:
        return None

    async def purge_source(self, source: SourceDescription) -> None:
        return None


async def test_one_page_emits_one_summary_and_one_row_per_failed_object() -> None:
    events: list[tuple[str, dict[str, Any]]] = []

    async def audit(action: str, payload: Any) -> None:
        events.append((action, dict(payload)))

    description = SourceDescription(
        connection_id="source", source_kind="test", account_id="a", supports_incremental=True
    )
    result = await ConnectedDataCoordinator(
        _Source(), _Ingest(), InMemorySourceSyncStore(), audit=audit
    ).run(description, agent_did=_DID, owner_id="w", limits=SyncLimits(retries=0))

    assert result.status is SyncStatus.COMPLETE
    summaries = [payload for action, payload in events if action == "connector.source.page"]
    assert len(summaries) == 1
    assert (summaries[0]["objects"], summaries[0]["failed"]) == (3, 1)
    assert summaries[0]["bytes"] > 0
    failed = [payload for action, payload in events if action.endswith(".object_failed")]
    assert len(failed) == 1
    assert not [action for action, _ in events if action.endswith("source.fetch")]


class _Sink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def write(self, event: Any) -> None:
        self.events.append(event)


class _Raising(_Source):
    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        raise SourceError(SourceFailureCode.TRANSIENT, "boom")


def _binding(source: _Source, sink: _Sink, *, active: bool = True) -> SourceAuthorizationBinding:
    async def grant_active() -> bool:
        return active

    return SourceAuthorizationBinding(
        source,
        connection_id="source",
        agent_did=_DID,
        tier="personal",
        grant_active=grant_active,
        audit_sink=sink,
    )


def _fetch(object_id: str) -> FetchSourceObject:
    return FetchSourceObject(
        connection_id="source", object_id=object_id, version="1", max_bytes=100
    )


async def test_routine_fetches_are_not_audited_one_by_one_but_still_checked() -> None:
    sink = _Sink()
    binding = _binding(_Source(), sink)

    for name in ("a", "b", "c"):
        await binding.fetch_source(_fetch(name))

    assert [e for e in sink.events if e.action == "connector.source.fetch"] == []


async def test_a_revoked_grant_still_audits_every_refused_fetch() -> None:
    sink = _Sink()
    binding = _binding(_Source(), sink, active=False)

    for name in ("a", "b"):
        try:
            await binding.fetch_source(_fetch(name))
        except SourceError:
            pass

    denied = [e for e in sink.events if e.action == "connector.source.fetch"]
    assert [(e.outcome, e.extra["reason"]) for e in denied] == [("deny", "grant_revoked")] * 2


async def test_a_failed_fetch_still_audits() -> None:
    sink = _Sink()
    binding = _binding(_Raising(), sink)

    try:
        await binding.fetch_source(_fetch("a"))
    except SourceError:
        pass

    errors = [e for e in sink.events if e.action == "connector.source.fetch"]
    assert [e.outcome for e in errors] == ["error"]
