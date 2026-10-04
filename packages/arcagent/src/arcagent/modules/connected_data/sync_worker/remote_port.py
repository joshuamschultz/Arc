"""The main process's ingest port: it reads a store locally and writes it through the worker.

``RemoteIngestPort`` implements the same ``IngestPort`` contract (and the same
optional hooks) as ``ArcMemoryIngestAdapter``, so the coordinator and
``ConnectedDataService`` cannot tell them apart. Every call that writes a
document store becomes one request to the sync worker. Every read (search,
listing, counts, the routing overview) runs here against a read-only handle on
the same store: the SQLite file opened ``mode=ro`` and ``query_only``, so the
main process holds no write connection to any connected-data store.

The control rows a port touches (the source incarnation, a remembered source
description, a staged mapping proposal) are arcstore rows the main process
already owns; they stay here.
"""

from __future__ import annotations

from collections.abc import Mapping
from importlib import import_module
from pathlib import Path
from typing import Any, Protocol

from arctrust.audit import AuditEvent, AuditSink, emit

from arcagent.connected_data import IngestPort, KnowledgeHome, MappingPlan
from arcagent.extension.source import SourceContent, SourceDescription, SourceObject
from arcagent.modules.connected_data.ingest import ArcMemoryIngestAdapter
from arcagent.modules.connected_data.sync_worker.specs import SealKey, StoreSpec


class WriterChannel(Protocol):
    """Where a store write goes: the supervised sync worker."""

    async def write(
        self,
        store: StoreSpec,
        method: str,
        args: dict[str, Any],
        body: bytes = b"",
        *,
        audit: AuditSink | None = None,
    ) -> tuple[Any, bytes]: ...


def replay_audit(events: Any, sink: AuditSink | None) -> None:
    """Emit the worker's audit events for a request through the caller's own sink."""
    if sink is None or not isinstance(events, list):
        return
    for raw in events:
        try:
            emit(AuditEvent.model_validate(raw), sink)
        except ValueError:
            continue


def seal_key_for(root: Path) -> SealKey | None:
    """The public half of the seal key this process pinned for ``root``, if any.

    The worker binds a signer for the store from it; every signature is still
    made here.
    """
    try:
        bound = import_module("arcmemory.okf_seal").bound_signer
    except ImportError:
        return None
    signer = bound(root)
    if signer is None:
        return None
    return SealKey(did=signer.did, public_key=signer.public_key.hex(), algorithm=signer.algorithm)


def _source(source: SourceDescription) -> dict[str, Any]:
    return source.model_dump(mode="json")


class RemoteIngestPort(IngestPort):
    """Read one store here; write it in the sync worker."""

    def __init__(
        self,
        local: ArcMemoryIngestAdapter,
        store: StoreSpec,
        channel: WriterChannel,
        *,
        audit_sink: AuditSink | None = None,
    ) -> None:
        self._local = local
        self._store = store
        self._channel = channel
        self._audit = audit_sink

    @property
    def store(self) -> StoreSpec:
        return self._store

    @property
    def local(self) -> ArcMemoryIngestAdapter:
        """The read-only handle on the same store."""
        return self._local

    async def _write(self, method: str, args: dict[str, Any], body: bytes = b"") -> Any:
        value, _ = await self._channel.write(self._store, method, args, body, audit=self._audit)
        return value

    # -- writes: every one goes to the worker ------------------------------

    async def require_approved_mapping(self, source: SourceDescription) -> MappingPlan:
        raw = await self._write("require_approved_mapping", {"source": _source(source)})
        return MappingPlan.model_validate(raw)

    async def ingest(
        self,
        source: SourceDescription,
        source_object: SourceObject,
        content: SourceContent | None,
        mapping: MappingPlan,
    ) -> None:
        await self._write(
            "ingest",
            {
                "source": _source(source),
                "object": source_object.model_dump(mode="json"),
                "content": None
                if content is None
                else content.model_dump(mode="json", exclude={"content"}),
                "mapping": mapping.model_dump(mode="json"),
            },
            b"" if content is None else content.content,
        )

    async def complete_snapshot(
        self, source: SourceDescription, object_ids: frozenset[str], mapping: MappingPlan
    ) -> None:
        await self._write(
            "complete_snapshot",
            {
                "source": _source(source),
                "object_ids": sorted(object_ids),
                "mapping": mapping.model_dump(mode="json"),
            },
        )

    async def finish_sync(self, source: SourceDescription) -> None:
        await self._write("finish_sync", {"source": _source(source)})

    async def refresh_operator_guide(self, source: SourceDescription) -> bool:
        return bool(await self._write("refresh_operator_guide", {"source": _source(source)}))

    async def relayout_source(self, source: SourceDescription) -> dict[str, int]:
        counts = await self._write("relayout_source", {"source": _source(source)})
        return _counts(counts)

    async def reset_source(self, source: SourceDescription) -> None:
        await self._write("reset_source", {"source": _source(source)})

    async def purge_source(self, source: SourceDescription) -> None:
        await self._write("purge_source", {"source": _source(source)})
        # The Brain lives here, not in the worker.
        await self._local.unregister_datastore(source)

    async def adopt_documents(
        self,
        source: SourceDescription,
        donor: RemoteIngestPort,
        donor_source: SourceDescription,
        *,
        dry_run: bool,
    ) -> dict[str, int]:
        counts = await self._write(
            "adopt_documents",
            {
                "source": _source(source),
                "donor": donor.store.model_dump(mode="json"),
                "donor_source": _source(donor_source),
                "dry_run": dry_run,
            },
        )
        return _counts(counts)

    async def claim_profile(self, profile: str) -> bool:
        """Claim (or compare) the shared store's embedding profile."""
        return bool(await self._write("claim_profile", {"profile": profile}))

    async def drop_store(self) -> None:
        """Delete a shared store nobody reads any more, root and all."""
        await self._write("drop_store", {})

    # -- reads and control rows: here ---------------------------------------

    async def aclose(self) -> None:
        await self._local.aclose()

    async def source_generation(self, source: SourceDescription) -> int:
        return await self._local.source_generation(source)

    async def remember_source(self, source: SourceDescription) -> None:
        await self._local.remember_source(source)

    async def remembered_source(self, connection_id: str) -> SourceDescription | None:
        return await self._local.remembered_source(connection_id)

    async def forget_source(self, connection_id: str) -> None:
        await self._local.forget_source(connection_id)

    async def stage_mapping(
        self, source: SourceDescription, homes: tuple[KnowledgeHome, ...]
    ) -> str:
        return await self._local.stage_mapping(source, homes)

    def allowed_homes(self, source: SourceDescription) -> tuple[KnowledgeHome, ...]:
        return self._local.allowed_homes(source)

    def canonical_source_id(self, source: SourceDescription) -> str:
        return self._local.canonical_source_id(source)

    async def documents_indexed(self, source: SourceDescription) -> int:
        return await self._local.documents_indexed(source)

    async def mapping_approval_status(self, approval_id: str) -> str:
        return await self._local.mapping_approval_status(approval_id)

    async def list_review_items(
        self, *, status: str | None = None, source_id: str | None = None
    ) -> list[Any]:
        return await self._local.list_review_items(status=status, source_id=source_id)

    async def resolve_review(self, review_id: str, decision: str) -> Any | None:
        return await self._local.resolve_review(review_id, decision)

    async def profile_context(self, profile_id: str, *, clearance: str = "unclassified") -> Any:
        return await self._local.profile_context(profile_id, clearance=clearance)

    async def profile_recall(
        self, profile_id: str, query: str, *, clearance: str = "unclassified"
    ) -> list[Any]:
        return await self._local.profile_recall(profile_id, query, clearance=clearance)

    async def approved_mapping(self, source: SourceDescription) -> MappingPlan | None:
        return await self._local.approved_mapping(source)

    async def search_pool(
        self, query: str, source_id: str, *, clearance: str, top_k: int | None
    ) -> list[Any]:
        return await self._local.search_pool(query, source_id, clearance=clearance, top_k=top_k)

    async def list_pool(self, source_id: str, *, limit: int) -> list[Any]:
        return await self._local.list_pool(source_id, limit=limit)

    async def root_overview(
        self, source: SourceDescription
    ) -> tuple[list[tuple[str, int]], list[str]]:
        return await self._local.root_overview(source)

    async def register_datastore(
        self, source: SourceDescription, adapter: Any, mapping: MappingPlan
    ) -> None:
        # A datastore port attaches to the agent's live Brain, which is here.
        await self._local.register_datastore(source, adapter, mapping)


def _counts(raw: Any) -> dict[str, int]:
    if not isinstance(raw, Mapping):
        return {}
    return {str(key): int(value) for key, value in raw.items()}


__all__ = ["RemoteIngestPort", "WriterChannel", "replay_audit", "seal_key_for"]
