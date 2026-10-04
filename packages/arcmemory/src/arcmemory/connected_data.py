"""Fail-closed connected-source mapping and document ingestion."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from arcokf import listable_dir, listable_file, validate_folder_index
from arcstore.approvals import ApprovalStore
from arctrust.audit import AuditEvent, AuditSink, emit
from arctrust.classification import parse_classification
from pydantic import BaseModel, ConfigDict, Field

from arcmemory.blob_ontology import BlobObject, walk_blob_source
from arcmemory.chunk import RecursiveChunker
from arcmemory.collection_index import source_maintainer
from arcmemory.config import MemoryConfig
from arcmemory.connected_layout import flat_name, prune_empty_dirs, target_path
from arcmemory.db import Durability, MemoryDB
from arcmemory.doc_index import DocHit, DocIndex, object_key
from arcmemory.extract import ExtractionUnavailable, get_extractor
from arcmemory.index.graph import WeightedGraph
from arcmemory.index.rebuild import Embedder
from arcmemory.index.source import SourceChunk
from arcmemory.ingest import deterministic_event_id, ingest_batch
from arcmemory.mapping import (
    commit_mapping,
    load_committed_mapping,
    mapping_call_hash,
    stage_mapping_proposal,
)
from arcmemory.mdfile import atomic_write_text, parse_document, read_frontmatter, render_document
from arcmemory.profile import ProfileFactKind, ProfileReviewStore, ReviewPort
from arcmemory.security import content_hash, document_sanitize
from arcmemory.source_guide import (
    GUIDE_DOCUMENT,
    SourceGuideTamperedError,
    sync_guide_document,
)
from arcmemory.stores.episodic import EpisodicStore
from arcmemory.stores.provenance import ProvenanceStore
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import MemoryHome, Provenance, Scope, SourceMapping, SourceRecord

_logger = logging.getLogger("arcmemory.connected_data")


class ConnectedSourceShape(StrEnum):
    """Vendor-neutral data shape used to route a source into ArcMemory."""

    DOCUMENT = "document"
    MAIL = "mail"
    DATASTORE = "datastore"
    BLOB = "blob"
    PROFILE = "profile"


class ConnectedSource(BaseModel):
    """Vendor-neutral connected-source identity and capabilities."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    connection_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    source_kind: str = Field(min_length=1)
    data_shape: ConnectedSourceShape = ConnectedSourceShape.DOCUMENT
    supports_incremental: bool = True
    supports_deletes: bool = True
    generation: int = Field(default=1, ge=1)


class ConnectedObject(BaseModel):
    """One discovered source object, including deletion tombstones."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    object_id: str = Field(min_length=1)
    locator: str = Field(min_length=1)
    version: str = Field(min_length=1)
    media_type: str = ""
    kind: str = "file"
    deleted: bool = False
    classification: str = ""
    revision: int | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class SourceContent(BaseModel):
    """Fetched bytes for one exact object version."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    object_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    media_type: str = ""
    content: bytes


class ApprovedMapping(BaseModel):
    """Canonical proposal plus the separate approval identity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mapping_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    homes: list[MemoryHome] = Field(default_factory=list, min_length=1)
    revision: str = Field(min_length=1)
    content_hash: str = Field(min_length=1)


class ConnectedObjectState(BaseModel):
    """Object membership supplied by the durable source-sync state owner."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    revision: int | None = None
    path: str = ""
    deleted: bool = False


class DocumentStatus(StrEnum):
    """Lifecycle state of an extracted connected document."""

    INDEXED = "indexed"
    MISSING = "missing"


class ConnectedDocument(BaseModel):
    """An operator-safe document inventory row; use document search for content."""

    source_id: str
    object_id: str
    version: str
    classification: str
    content_hash: str
    path: str
    status: DocumentStatus = DocumentStatus.INDEXED


class RelayoutReport(BaseModel):
    """What one ``relayout_source`` pass changed; all zeros means nothing was left to do."""

    model_config = ConfigDict(frozen=True)

    scanned: int = 0
    moved: int = 0
    repathed: int = 0
    state_updated: int = 0
    refused: int = 0


class AdoptionReport(BaseModel):
    """What one ``adopt_documents`` pass did (or, on a dry run, would do).

    ``adopted`` documents were re-keyed into this store from the donor's extracted
    text, so no provider was asked for them again; ``deduplicated`` ones were
    already here at the same version; ``skipped`` ones could not be read.
    """

    model_config = ConfigDict(frozen=True)

    documents: int = 0
    adopted: int = 0
    deduplicated: int = 0
    skipped: int = 0


class MappingAuthority(Protocol):
    """Who authorizes writes to a store no single agent owns (P18-4).

    A connection-scoped store is written by whichever subscribed agent holds the
    connection's sync lease. Its own approval row cannot name the shared store, so
    the writer delegates: ``authorized_homes`` answers the id of the writer's
    verified approval and the homes it allows, or ``None`` when nothing
    authorizes the store any more. Asked again before every write.
    """

    async def authorized_homes(self) -> tuple[str, tuple[MemoryHome, ...]] | None: ...


class _Move(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    object_id: str
    current: Path
    target: Path
    refused: bool = False


class ConnectedObjectStatePort(Protocol):
    """Narrow object-state seam; production may implement it in ArcStore."""

    async def get_object_state(
        self, source_id: str, object_id: str
    ) -> ConnectedObjectState | None: ...

    async def put_object_state(
        self, source_id: str, object_id: str, state: ConnectedObjectState
    ) -> None: ...

    async def list_object_ids(self, source_id: str) -> list[str]: ...

    async def clear_source(self, source_id: str) -> None: ...


class InMemoryObjectState:
    """Non-durable test default; restart-safe deployments inject ArcStore state."""

    def __init__(self) -> None:
        self._states: dict[tuple[str, str], ConnectedObjectState] = {}

    async def get_object_state(
        self, source_id: str, object_id: str
    ) -> ConnectedObjectState | None:
        return self._states.get((source_id, object_id))

    async def put_object_state(
        self, source_id: str, object_id: str, state: ConnectedObjectState
    ) -> None:
        self._states[(source_id, object_id)] = state

    async def list_object_ids(self, source_id: str) -> list[str]:
        return [
            object_id for (stored_source, object_id) in self._states if stored_source == source_id
        ]

    async def clear_source(self, source_id: str) -> None:
        self._states = {key: value for key, value in self._states.items() if key[0] != source_id}

    async def source_generation(self, connection_id: str) -> int:
        del connection_id
        return 1


class SourceMappingPendingError(RuntimeError):
    """No exact operator-approved mapping exists yet."""


class SourceMappingDeniedError(RuntimeError):
    """The exact mapping proposal was denied or expired."""


class ConnectedObjectError(RuntimeError):
    """A source object could not be safely represented or indexed."""


class UnsupportedConnectedObjectError(ConnectedObjectError):
    """The object media type is outside the explicit extractor allowlist."""


class ConnectedObjectTooLargeError(ConnectedObjectError):
    """The fetched object exceeds the configured byte bound."""


class ConnectedObjectOrderError(ConnectedObjectError):
    """A changed opaque vendor version has no trusted monotonic ordering."""


def source_instance_id(agent_did: str, source: ConnectedSource) -> str:
    """Return a collision-resistant, non-secret source-instance identifier."""
    raw = "\0".join((agent_did, source.connection_id, source.account_id, str(source.generation)))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _citation_metadata(source: ConnectedSource, source_object: ConnectedObject) -> dict[str, str]:
    """What an agent needs to cite a document: title, kind, url/locator, updated time.

    Kept in the extracted document's front matter (never in the embedded text),
    so a hit can name its source without the citation skewing relevance.
    """
    extra = source_object.metadata
    url = extra.get("url") or (source_object.locator if "://" in source_object.locator else "")
    citation = {
        "title": extra.get("title", ""),
        "source_kind": source.source_kind,
        "url": url,
        "locator": source_object.locator,
        "updated_at": extra.get("modified_at") or extra.get("updated_at", ""),
    }
    return {key: value for key, value in citation.items() if value}


def _write_document(
    path: Path, metadata: dict[str, Any], text: str, stale: str, root: Path
) -> None:
    """Render and atomically write one extracted document; drop a moved predecessor."""
    atomic_write_text(path, render_document(metadata, text))
    if stale:
        _remove_document_file(Path(stale), root)


def _remove_document_file(path: Path, root: Path) -> None:
    """Delete one document file and the folders its removal leaves empty."""
    path.unlink(missing_ok=True)
    prune_empty_dirs(path.parent, root)


class _HeldAudit:
    """Collect audit events raised in a worker thread for emission on the loop.

    Sinks are written from the event-loop thread everywhere else; a sink is not
    required to be thread-safe, so an offloaded step hands its events back.
    """

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self._events.append(event)

    def release(self, sink: AuditSink | None) -> None:
        events, self._events = self._events, []
        if sink is None:
            return
        for event in events:
            emit(event, sink)


class ConnectedDataService:
    """Approval-gated, idempotent connected-document writer for one agent."""

    def __init__(
        self,
        workspace: Path,
        agent_did: str,
        *,
        approval_store: ApprovalStore | None,
        object_state: ConnectedObjectStatePort | None = None,
        config: MemoryConfig | None = None,
        embedder: Embedder | None = None,
        audit_sink: AuditSink | None = None,
        review_port: ReviewPort | None = None,
        authority: MappingAuthority | None = None,
        durability: Durability = "full",
        read_only: bool = False,
    ) -> None:
        self._workspace = Path(workspace)
        #: A reader that may search and list but never write: the main process,
        #: while the sync worker owns every connected-data store write.
        self._read_only = read_only
        self._agent_did = agent_did
        self._approval = approval_store
        #: Set only for a connection-scoped store (P18-4): its writes are authorized
        #: by the writing subscriber's verified approval, never by an approval row
        #: of its own, and it is never staged for approval.
        self._authority = authority
        self._config = config or MemoryConfig()
        self._audit = audit_sink
        self._db = MemoryDB(self._workspace, durability=durability, read_only=read_only)
        self._embedder = embedder
        self._object_state = object_state or InMemoryObjectState()
        self._reviews = review_port or ProfileReviewStore(
            self._workspace, agent_did=self._agent_did, audit_sink=self._audit
        )
        # Per-run cache of mapping ``call_hash`` values already verified approved.
        # The service is built fresh per sync run (arcagent's ingest factory), so
        # this is a run-scoped cache: approval is verified once per mapping per
        # run instead of once per object. Keyed by ``call_hash`` so a structural
        # change re-derives a new hash and re-triggers approval — the durable-
        # approval invariant is preserved, and one mapping's approval can never
        # authorize a structurally different mapping.
        self._approved_call_hashes: set[str] = set()

    def _source_id(self, source: ConnectedSource) -> str:
        return source_instance_id(self._agent_did, source)

    def _require_writable(self) -> None:
        """Refuse a write through a read-only handle, before it touches anything."""
        if self._read_only:
            raise PermissionError("this connected-data store handle is read-only")

    def allowed_homes(self, source: ConnectedSource) -> tuple[MemoryHome, ...]:
        """Return destinations compatible with a source without vendor coupling."""
        if source.data_shape is ConnectedSourceShape.MAIL:
            return (MemoryHome.MEMORY, MemoryHome.DOCUMENT)
        if source.data_shape is ConnectedSourceShape.DATASTORE:
            return (MemoryHome.DATASTORE, MemoryHome.PROFILE)
        if source.data_shape is ConnectedSourceShape.PROFILE:
            return (MemoryHome.PROFILE, MemoryHome.DOCUMENT)
        if source.data_shape is ConnectedSourceShape.BLOB:
            return (MemoryHome.BLOB, MemoryHome.DOCUMENT)
        if source.data_shape is ConnectedSourceShape.DOCUMENT:
            return (MemoryHome.DOCUMENT,)
        return ()

    def _proposal(
        self, source: ConnectedSource, homes: tuple[MemoryHome, ...] | None = None
    ) -> SourceMapping:
        allowed = self.allowed_homes(source)
        selected = allowed if homes is None else tuple(dict.fromkeys(homes))
        if not selected or not set(selected).issubset(allowed):
            raise SourceMappingDeniedError("mapping selects an unsupported destination")
        canonical = json.dumps(
            {
                "account_id": source.account_id,
                "connection_id": source.connection_id,
                "homes": [home.value for home in selected],
                "source_kind": source.source_kind,
                "data_shape": source.data_shape.value,
                "supports_deletes": source.supports_deletes,
                "supports_incremental": source.supports_incremental,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        revision = content_hash(canonical)
        return SourceMapping(
            source_id=self._source_id(source),
            homes=list(selected),
            revision=revision,
            content_hash=content_hash(canonical + "\0" + revision),
        )

    async def propose_mapping(self, source: ConnectedSource, homes: tuple[str, ...]) -> str:
        """Stage an exact, operator-selected mapping proposal."""
        if self._authority is not None:
            # A shared store is approved through its subscribers, one agent at a time.
            raise SourceMappingDeniedError("a connection-scoped store is never staged")
        try:
            selected = tuple(MemoryHome(home) for home in homes)
        except ValueError as exc:
            raise SourceMappingDeniedError("mapping selects an unknown destination") from exc
        proposal = self._proposal(source, selected)
        if self._approval is None:
            raise SourceMappingPendingError()
        return await stage_mapping_proposal(
            proposal, approval_store=self._approval, agent_did=self._agent_did
        )

    async def require_approved_mapping(self, source: ConnectedSource) -> ApprovedMapping:
        """Load an exact active approval, or stage one safe default proposal."""
        self._require_writable()
        await self._require_current_generation(source)
        if self._authority is not None:
            return await self._delegated_mapping(source, self._authority)
        source_id = self._source_id(source)
        register = SemanticStore(
            self._workspace,
            # Registration is a source fact; no raw account or locator is persisted.
            WeightedGraph(self._db),
            self._agent_did,
        )
        register.write_fact(
            f"source-{source_id}", "kind", source.source_kind, entity_type="source"
        )
        if self._approval is None:
            raise SourceMappingPendingError()
        approved = await self._approved_row(self._approval, source)
        if approved is not None:
            approval_id, proposal = approved
            commit_mapping(proposal, store=register)
            committed = load_committed_mapping(proposal.source_id, store=register)
            if committed is None or committed.revision != proposal.revision:
                raise SourceMappingDeniedError()
            return ApprovedMapping(
                mapping_id=approval_id,
                source_id=committed.source_id,
                homes=committed.homes,
                revision=committed.revision,
                content_hash=committed.content_hash,
            )
        proposal = self._proposal(source)
        await stage_mapping_proposal(
            proposal,
            approval_store=self._approval,
            agent_did=self._agent_did,
        )
        raise SourceMappingPendingError()

    async def find_approved_mapping(self, source: ConnectedSource) -> ApprovedMapping | None:
        """The operator-approved mapping of this source, or ``None``; stages nothing.

        The read-only half of :meth:`require_approved_mapping`, for a caller that
        must know whether a mapping is approved without proposing one when it is
        not (deciding which store a granted agent reads, P18-4).
        """
        await self._require_current_generation(source)
        if self._authority is not None or self._approval is None:
            return None
        approved = await self._approved_row(self._approval, source)
        if approved is None:
            return None
        approval_id, proposal = approved
        return ApprovedMapping(
            mapping_id=approval_id,
            source_id=proposal.source_id,
            homes=list(proposal.homes),
            revision=proposal.revision,
            content_hash=proposal.content_hash,
        )

    async def _approved_row(
        self, approval: ApprovalStore, source: ConnectedSource
    ) -> tuple[str, SourceMapping] | None:
        """The approved row whose exact proposal matches this source, if any."""
        await approval.start()
        for row in await approval.list(status="approved"):
            try:
                home_values = row.arguments.get("homes", "").split(",")
                homes = tuple(MemoryHome(home) for home in home_values if home)
                proposal = self._proposal(source, homes)
            except (SourceMappingDeniedError, ValueError):
                continue
            target = mapping_call_hash(
                proposal.source_id,
                proposal.homes,
                revision=proposal.revision,
                content_hash_value=proposal.content_hash,
            )
            # ``expires_at`` bounds only the *pending* window (an unacted request
            # auto-cancels). An operator-approved mapping is durable: it lapses
            # only when the mapping structure changes, which re-derives a new
            # ``call_hash`` above and re-triggers approval — never on a timer.
            if row.call_hash == target:
                return row.id, proposal
        return None

    async def _delegated_mapping(
        self, source: ConnectedSource, authority: MappingAuthority
    ) -> ApprovedMapping:
        """Commit a shared store's mapping from the writer's verified approval."""
        grant = await authority.authorized_homes()
        if grant is None:
            raise SourceMappingDeniedError("no subscriber authorizes this shared source")
        approval_id, homes = grant
        proposal = self._proposal(source, homes)
        register = SemanticStore(self._workspace, WeightedGraph(self._db), self._agent_did)
        register.write_fact(
            f"source-{proposal.source_id}", "kind", source.source_kind, entity_type="source"
        )
        commit_mapping(proposal, store=register)
        committed = load_committed_mapping(proposal.source_id, store=register)
        if committed is None or committed.revision != proposal.revision:
            raise SourceMappingDeniedError()
        return ApprovedMapping(
            mapping_id=approval_id,
            source_id=committed.source_id,
            homes=committed.homes,
            revision=committed.revision,
            content_hash=committed.content_hash,
        )

    async def object_version(self, source: ConnectedSource, object_id: str) -> str | None:
        """The version this store holds for one object, or ``None`` (absent or deleted)."""
        state = await self._object_state.get_object_state(self._source_id(source), object_id)
        return None if state is None or state.deleted else state.version

    async def adopt_documents(
        self,
        source: ConnectedSource,
        donor: ConnectedDataService,
        donor_source: ConnectedSource,
        *,
        dry_run: bool,
    ) -> AdoptionReport:
        """Re-key another store's extracted documents into this one, without a fetch.

        The P18-4 migration: an agent's own store of a connection becomes part of
        the connection's shared store. Each document is read from the donor's
        extracted text and written, chunked and recorded here under this store's
        source id, carrying its version so the next sync does not fetch it again.
        A document already here at the same version is deduplicated. Only the
        DOCUMENT home moves; a dry run counts and writes nothing.
        """
        self._require_writable()
        mapping = await self.require_approved_mapping(source)
        if MemoryHome.DOCUMENT not in mapping.homes:
            raise SourceMappingDeniedError("shared store does not hold documents")
        source_id = self._source_id(source)
        counts = {"documents": 0, "adopted": 0, "deduplicated": 0, "skipped": 0}
        for document in await donor.list_documents(donor_source):
            counts["documents"] += 1
            prior = await self._object_state.get_object_state(source_id, document.object_id)
            if prior is not None and not prior.deleted and prior.version == document.version:
                counts["deduplicated"] += 1
                continue
            if dry_run:
                counts["adopted"] += 1
                continue
            adopted = await self._adopt_one(source_id, donor, donor_source, document, prior)
            counts["adopted" if adopted else "skipped"] += 1
        if not dry_run and counts["adopted"]:
            await self.finish_sync(source)
        return AdoptionReport(**counts)

    async def _adopt_one(
        self,
        source_id: str,
        donor: ConnectedDataService,
        donor_source: ConnectedSource,
        document: ConnectedDocument,
        prior: ConnectedObjectState | None,
    ) -> bool:
        """Write one donor document here: same body, same citation, this store's id."""
        donor_id = donor._source_id(donor_source)
        donor_root = donor._document_root(donor_id)
        donor_path = donor._workspace / document.path
        root = self._document_root(source_id)
        try:
            relative = donor_path.relative_to(donor_root)
            text = await asyncio.to_thread(donor_path.read_text, encoding="utf-8")
            metadata, body = parse_document(text)
        except (OSError, UnicodeDecodeError, ValueError):
            return False
        target = root / relative
        if not target.resolve().is_relative_to(root.resolve()):
            return False
        donor_state = await donor._object_state.get_object_state(donor_id, document.object_id)
        stale = prior.path if prior is not None and prior.path != target.as_posix() else ""
        rekeyed = {**metadata, "source": source_id}
        await asyncio.to_thread(_write_document, target, rekeyed, body, stale, root)
        source_object = ConnectedObject(
            object_id=document.object_id,
            locator=str(rekeyed.get("locator") or document.object_id),
            version=document.version,
            classification=document.classification,
        )
        chunks = await self._document_chunks(source_id, source_object, body, target)
        index = self._doc_index()
        await index.delete_object(source_id, self._agent_did, document.object_id)
        await index.index_source(source_id, self._agent_did, chunks)
        provenance = ProvenanceStore(self._db)
        provenance.remove(source_id, document.object_id)
        provenance.record(
            document.content_hash or content_hash(body),
            Provenance(
                source=source_id,
                external_id=document.object_id,
                classification=document.classification,
            ),
        )
        await self._object_state.put_object_state(
            source_id,
            document.object_id,
            ConnectedObjectState(
                version=document.version,
                revision=donor_state.revision if donor_state is not None else None,
                path=target.as_posix(),
            ),
        )
        self._audit_object(source_id, source_object, "adopted")
        return True

    async def ingest(
        self,
        source: ConnectedSource,
        source_object: ConnectedObject,
        content: SourceContent | None,
        mapping: ApprovedMapping,
    ) -> None:
        """Safely replace one object version after verifying its exact mapping."""
        self._require_writable()
        await self._require_current_generation(source)
        proposal = self._proposal(source, tuple(mapping.homes))
        if (
            mapping.source_id != proposal.source_id
            or mapping.revision != proposal.revision
            or mapping.content_hash != proposal.content_hash
            or not mapping.homes
        ):
            raise SourceMappingDeniedError()
        if not await self._mapping_is_approved(mapping):
            raise SourceMappingDeniedError()
        source_id = self._source_id(source)
        try:
            # Strict is the federal posture, matching every other classification
            # read in this service. Hardcoded here it refused every object from a
            # connector that labels nothing — which is every connector at personal
            # tier, so no connected source could ever finish a sync.
            parse_classification(
                source_object.classification, strict=self._config.tier == "federal"
            )
        except ValueError as exc:
            self._audit_object(source_id, source_object, "skipped", "invalid_classification")
            raise ConnectedObjectError("connected object classification is invalid") from exc
        prior = await self._object_state.get_object_state(source_id, source_object.object_id)
        if prior is not None and not prior.deleted:
            if prior.version == source_object.version:
                return
            if (
                prior.revision is None
                or source_object.revision is None
                or source_object.revision <= prior.revision
            ):
                self._audit_object(source_id, source_object, "skipped", "stale_revision")
                raise ConnectedObjectOrderError(
                    "connected object update lacks a newer monotonic revision"
                )
        index = self._doc_index()
        if source_object.deleted:
            # Delete all destination state, not merely routes in the newest mapping:
            # an operator can legitimately remap a source between syncs. The
            # source's routing index catches up once, in ``finish_sync``.
            await index.delete_object(source_id, self._agent_did, source_object.object_id)
            await asyncio.to_thread(self._remove_file, source_id, prior)
            EpisodicStore(self._db, self._workspace).delete(
                Scope(agent_did=self._agent_did).key,
                deterministic_event_id(source_id, source_object.object_id),
            )
            await self._reviews.revoke_source(source_id, source_object.object_id)
            await self._remove_blob_object(source_id, source_object.object_id)
            ProvenanceStore(self._db).remove(source_id, source_object.object_id)
            await self._object_state.put_object_state(
                source_id,
                source_object.object_id,
                ConnectedObjectState(
                    version=source_object.version,
                    revision=source_object.revision,
                    deleted=True,
                ),
            )
            self._audit_object(source_id, source_object, "deleted")
            return
        if content is None or content.object_id != source_object.object_id:
            self._audit_object(source_id, source_object, "skipped", "missing_content")
            raise ConnectedObjectError("connected object content is missing")
        if content.version != source_object.version:
            self._audit_object(source_id, source_object, "skipped", "version_mismatch")
            raise ConnectedObjectError("connected object content version does not match discovery")
        if len(content.content) > self._config.backfill_max_object_bytes:
            self._audit_object(source_id, source_object, "skipped", "byte_limit")
            raise ConnectedObjectTooLargeError("connected object exceeds byte limit")
        extractor = get_extractor(
            content.media_type or source_object.media_type,
            filename=source_object.locator,
        )
        if extractor is None:
            self._audit_object(source_id, source_object, "skipped", "unsupported_type")
            raise UnsupportedConnectedObjectError("connected object media type is unsupported")
        try:
            # Parsing is CPU-bound third-party code over someone else's file; on
            # the event loop one large PDF froze chat, NATS and health with it.
            extracted = await asyncio.to_thread(
                extractor.extract, content.content, filename=source_object.locator
            )
        except ExtractionUnavailable as exc:
            self._audit_object(source_id, source_object, "skipped", "extractor_unavailable")
            raise ConnectedObjectError(str(exc)) from exc
        except Exception as exc:
            # A document parser is third-party code run over a file someone else
            # produced; it raises whatever it likes. Listing a few exception
            # types let one malformed PDF through as a hard failure that ended
            # the whole sync, so an account was indexed as far as its first bad
            # file and no further. An unreadable document is one skipped object.
            self._audit_object(source_id, source_object, "skipped", "extraction_failed")
            raise ConnectedObjectError(
                f"connected object extraction failed: {type(exc).__name__}"
            ) from exc
        clean, digest = await self._sanitized(extracted)
        profile_candidate = self._profile_candidate(source_object, mapping.homes)
        path = self._document_target(source_id, source_object, prior)
        if MemoryHome.DOCUMENT in mapping.homes:
            await self._write_and_index_document(
                index, source_id, source, source_object, clean, digest, path, prior
            )
        else:
            await index.delete_object(source_id, self._agent_did, source_object.object_id)
            await asyncio.to_thread(self._remove_file, source_id, prior)
        if MemoryHome.MEMORY in mapping.homes:
            ingest_batch(
                self._db,
                self._workspace,
                Scope(agent_did=self._agent_did),
                self._config,
                source_id,
                [
                    SourceRecord(
                        external_id=source_object.object_id,
                        text=clean,
                        kind=source_object.kind,
                        classification=source_object.classification,
                        source_updated_at=source_object.version,
                    )
                ],
            )
        if MemoryHome.PROFILE in mapping.homes:
            if profile_candidate is None:
                raise ConnectedObjectError("profile destination was not preflighted")
            await self._stage_profile_fact(source_id, source_object, clean, profile_candidate)
        if MemoryHome.BLOB in mapping.homes:
            await asyncio.to_thread(self._write_blob_object, source_id, source_object)
        ProvenanceStore(self._db).remove(source_id, source_object.object_id)
        ProvenanceStore(self._db).record(
            digest,
            Provenance(
                source=source_id,
                external_id=source_object.object_id,
                classification=source_object.classification,
            ),
        )
        await self._object_state.put_object_state(
            source_id,
            source_object.object_id,
            ConnectedObjectState(
                version=source_object.version,
                revision=source_object.revision,
                path=path.as_posix() if MemoryHome.DOCUMENT in mapping.homes else "",
            ),
        )
        self._audit_object(source_id, source_object, "indexed")

    async def _sanitized(self, extracted: str) -> tuple[str, str]:
        """Sanitize and hash an extracted body in a worker thread.

        NFKC normalization, the regex passes and SHA-256 over a megabyte body
        are CPU work; on the loop they stalled chat, NATS and health once per
        large document. An injection audit raised there is held and emitted
        here, on the loop, so sinks only ever see one thread.
        """
        held = _HeldAudit()

        def run() -> tuple[str, str]:
            clean = document_sanitize(
                extracted,
                actor_did=self._agent_did,
                tier=self._config.tier,
                audit_sink=held if self._audit is not None else None,
            )
            return clean, content_hash(clean)

        result = await asyncio.to_thread(run)
        held.release(self._audit)
        return result

    async def reset_source(self, source: ConnectedSource) -> None:
        """Clear a source snapshot while preserving its approved mapping."""
        self._require_writable()
        await self._clear_source(source, remove_mapping=False)

    async def complete_snapshot(
        self,
        source: ConnectedSource,
        object_ids: frozenset[str],
        mapping: ApprovedMapping,
    ) -> None:
        """Delete active objects absent from one fully successful source snapshot."""
        self._require_writable()
        await self._require_current_generation(source)
        proposal = self._proposal(source, tuple(mapping.homes))
        if (
            mapping.source_id != proposal.source_id
            or mapping.revision != proposal.revision
            or mapping.content_hash != proposal.content_hash
            or not await self._mapping_is_approved(mapping)
        ):
            raise SourceMappingDeniedError()
        source_id = self._source_id(source)
        for object_id in await self._object_state.list_object_ids(source_id):
            prior = await self._object_state.get_object_state(source_id, object_id)
            if prior is None or prior.deleted or object_id in object_ids:
                continue
            await self.ingest(
                source,
                ConnectedObject(
                    object_id=object_id,
                    locator=object_id,
                    version=f"snapshot-deleted:{prior.version}",
                    deleted=True,
                    classification="UNCLASSIFIED",
                    revision=None if prior.revision is None else prior.revision + 1,
                ),
                None,
                mapping,
            )

    async def finish_sync(self, source: ConnectedSource) -> None:
        """Bring source-wide derived state up to date, once per sync run.

        The routing ``index.md`` and the blob folder ontology each describe the
        whole source. Rebuilding either per object made one object cost the
        size of the inventory; the sync coordinator calls this after its pages
        instead. Only what already landed is reorganized — no new data enters
        here, so no mapping is consulted — but a fenced (stale) worker is still
        refused.
        """
        self._require_writable()
        await self._require_current_generation(source)
        source_id = self._source_id(source)
        root = self._document_root(source_id)
        if root.is_dir():
            await self._sync_operator_guide(source, root)
            await self._doc_index().refresh_collection_index(source_id, self._agent_did, root)
        if self._blob_inventory_root(source_id).is_dir():
            await asyncio.to_thread(self._reconcile_blob_inventory, source_id)

    async def refresh_operator_guide(self, source: ConnectedSource) -> bool:
        """Bring the source's operator guide into its routing index; ``True`` if it changed.

        The guide is edited in ArcUI, not synced from the provider, so it changes
        between sync runs. This rebuilds the root ``index.md`` through the same
        refresh a sync run ends with, and only when the guide document changed.
        """
        self._require_writable()
        await self._require_current_generation(source)
        source_id = self._source_id(source)
        root = self._document_root(source_id)
        if not root.is_dir() or not await self._sync_operator_guide(source, root):
            return False
        await self._doc_index().refresh_collection_index(source_id, self._agent_did, root)
        return True

    async def root_overview(
        self, source: ConnectedSource
    ) -> tuple[list[tuple[str, int]], list[str]]:
        """Top-level folders (with document counts) and document titles of one source.

        Read from the source's verified root ``index.md`` only; an unverified or
        absent index is an empty overview, never a guess. The operator guide
        document is left out: it describes the source, it is not in it.
        """
        root = self._document_root(self._source_id(source))
        validation = await asyncio.to_thread(validate_folder_index, root)
        if not validation.valid:
            return [], []
        folders = [
            (entry.path.split("/", 1)[0], entry.count)
            for entry in validation.entries
            if entry.is_folder
        ]
        titles = [
            entry.title
            for entry in validation.entries
            if not entry.is_folder and entry.path != GUIDE_DOCUMENT
        ]
        return folders, titles

    async def _sync_operator_guide(self, source: ConnectedSource, root: Path) -> bool:
        """Write the verified guide document into ``root``; a tamper drops it, loudly.

        A tampered guide must never stop the sync that found it (the data is still
        the operator's), but it must never be served either: the document is
        removed, the refusal is audited and logged at WARNING.
        """
        try:
            return await asyncio.to_thread(sync_guide_document, root, source.connection_id)
        except SourceGuideTamperedError as exc:
            _logger.warning(
                "operator guide for connection %r refused: %s", source.connection_id, exc
            )
            self._audit_guide_tampered(source)
            return True

    def _audit_guide_tampered(self, source: ConnectedSource) -> None:
        if self._audit is None:
            return
        emit(
            AuditEvent(
                actor_did=self._agent_did,
                action="connected_data.guide.tampered",
                target=source.connection_id,
                outcome="deny",
                extra={"reason": "signature_mismatch"},
            ),
            self._audit,
        )

    async def purge_source(self, source: ConnectedSource) -> None:
        """Irreversibly remove every retrievable artifact of a disconnected source."""
        self._require_writable()
        await self._clear_source(source, remove_mapping=True)

    def close(self) -> None:
        """Release this service's SQLite connection; a later call reopens it."""
        self._db.close()

    def _doc_index(self) -> DocIndex:
        return DocIndex(
            self._db,
            self._workspace,
            self._config,
            embedder=self._embedder,
            audit_sink=self._audit,
        )

    async def _clear_source(self, source: ConnectedSource, *, remove_mapping: bool) -> None:
        source_id = self._source_id(source)
        await self._doc_index().delete_source(source_id, self._agent_did)
        for object_id in await self._object_state.list_object_ids(source_id):
            EpisodicStore(self._db, self._workspace).delete(
                Scope(agent_did=self._agent_did).key,
                deterministic_event_id(source_id, object_id),
            )
        await self._reviews.revoke_all_for_source(source_id)
        ProvenanceStore(self._db).remove_source(source_id)
        await asyncio.to_thread(shutil.rmtree, self._document_root(source_id), True)
        await asyncio.to_thread(shutil.rmtree, self._blob_inventory_root(source_id), True)
        await self._object_state.clear_source(source_id)
        store = SemanticStore(self._workspace, WeightedGraph(self._db), self._agent_did)
        for slug in self.blob_folders(source):
            store.remove(slug)
        if remove_mapping:
            store.remove(f"source-{source_id}")
            store.remove(f"mapping-{source_id}")

    async def _require_current_generation(self, source: ConnectedSource) -> None:
        """Reject a stale worker after disconnect/reconnect has fenced its source."""
        generation = getattr(self._object_state, "source_generation", None)
        if not callable(generation):
            return
        if await generation(source.connection_id) != source.generation:
            raise SourceMappingDeniedError("source incarnation is no longer active")

    @property
    def review_port(self) -> ReviewPort:
        """The typed operator-review seam for provenance-bearing profile facts."""
        return self._reviews

    def _profile_candidate(
        self, source_object: ConnectedObject, homes: list[MemoryHome]
    ) -> tuple[str, str, ProfileFactKind] | None:
        """Validate every profile destination requirement before any sink writes."""
        if MemoryHome.PROFILE not in homes:
            return None
        profile_id = source_object.metadata.get("profile_id", "")
        field = source_object.metadata.get("profile_field", "")
        kind_value = source_object.metadata.get("profile_kind", ProfileFactKind.INFERRED.value)
        if not profile_id or not field:
            raise ConnectedObjectError(
                "profile ingestion requires profile_id and profile_field metadata"
            )
        try:
            kind = ProfileFactKind(kind_value)
        except ValueError as exc:
            raise ConnectedObjectError("profile_kind is invalid") from exc
        return profile_id, field, kind

    async def _stage_profile_fact(
        self,
        source_id: str,
        source_object: ConnectedObject,
        text: str,
        candidate: tuple[str, str, ProfileFactKind],
    ) -> None:
        """Stage, never apply, one source-provided profile fact for review."""
        profile_id, field, kind = candidate
        await self._reviews.submit(
            profile_id=profile_id,
            field=field,
            value=text,
            kind=kind,
            provenance=Provenance(
                source=source_id,
                external_id=source_object.object_id,
                classification=source_object.classification,
            ),
            classification=source_object.classification,
        )

    async def _write_and_index_document(
        self,
        index: DocIndex,
        source_id: str,
        source: ConnectedSource,
        source_object: ConnectedObject,
        text: str,
        digest: str,
        path: Path,
        prior: ConnectedObjectState | None,
    ) -> None:
        """Atomically persist one extracted document and replace only its chunks.

        Only this object's own chunks are touched; the source's routing index
        catches up once per run in ``finish_sync``. Rendering, the file write
        and chunking run off the event loop (the index writes stay on it: the
        MemoryDB connection is bound to the loop thread).
        """
        metadata = {
            "type": "ConnectedDocument",
            "source": source_id,
            "external_id": source_object.object_id,
            "version": source_object.version,
            "classification": source_object.classification,
            "content_hash": digest,
            **_citation_metadata(source, source_object),
        }
        stale = prior.path if prior is not None and prior.path != path.as_posix() else ""
        await asyncio.to_thread(
            _write_document, path, metadata, text, stale, self._document_root(source_id)
        )
        chunks = await self._document_chunks(source_id, source_object, text, path)
        await index.delete_object(source_id, self._agent_did, source_object.object_id)
        await index.index_source(source_id, self._agent_did, chunks)

    async def _document_chunks(
        self, source_id: str, source_object: ConnectedObject, text: str, path: Path
    ) -> list[SourceChunk]:
        """Produce stable, source-qualified chunks from an already-sanitized body.

        Ids are ``<source_id>:<object_id>#<n>`` (see ``object_key``). The
        chunker runs in a worker thread; any audit event it raises is held and
        emitted here, on the loop, so sinks only ever see one thread.
        """
        held = _HeldAudit()
        chunker = RecursiveChunker(
            chunk_tokens=self._config.doc_chunk_tokens,
            overlap=self._config.doc_chunk_overlap,
            audit_sink=held if self._audit is not None else None,
        )
        chunks = await asyncio.to_thread(
            chunker.chunk,
            text,
            source_path=path.relative_to(self._workspace).as_posix(),
            classification=source_object.classification,
        )
        held.release(self._audit)
        stem = object_key(source_id, source_object.object_id)
        return [
            chunk.model_copy(update={"chunk_id": f"{stem}#{position}"})
            for position, chunk in enumerate(chunks)
        ]

    async def _remove_blob_object(self, source_id: str, object_id: str) -> None:
        """Remove a tombstoned object; folder facts reconcile in ``finish_sync``."""
        path = self._blob_inventory_path(source_id, object_id)
        await asyncio.to_thread(path.unlink, missing_ok=True)

    def _write_blob_object(self, source_id: str, source_object: ConnectedObject) -> None:
        blob = BlobObject(
            path=source_object.locator.lstrip("/"),
            mime=source_object.media_type,
            classification=source_object.classification,
        )
        atomic_write_text(
            self._blob_inventory_path(source_id, source_object.object_id),
            blob.model_dump_json() + "\n",
        )

    def _reconcile_blob_inventory(self, source_id: str) -> None:
        """Rebuild source folder facts from its complete live object inventory."""
        inventory = self._blob_inventory_root(source_id)
        objects: list[BlobObject] = []
        if inventory.is_dir():
            for path in sorted(inventory.glob("*.json")):
                try:
                    objects.append(
                        BlobObject.model_validate_json(path.read_text(encoding="utf-8"))
                    )
                except (OSError, ValueError):
                    continue
        store = SemanticStore(self._workspace, WeightedGraph(self._db), self._agent_did)
        walk_blob_source(
            objects,
            source_id=source_id,
            store=store,
            now=datetime.now(UTC).isoformat(),
        )

    def _blob_inventory_path(self, source_id: str, object_id: str) -> Path:
        object_key = hashlib.sha256(object_id.encode("utf-8")).hexdigest()
        return self._blob_inventory_root(source_id) / f"{object_key}.json"

    def _blob_inventory_root(self, source_id: str) -> Path:
        return self._workspace / "memory" / "blob_inventory" / source_id

    async def list_documents(self, source: ConnectedSource) -> list[ConnectedDocument]:
        """Return this source's indexed document inventory without exposing bodies.

        Reads every document's front matter, so it runs off the event loop: the
        operator card asks for this count after every sync run.
        """
        return await asyncio.to_thread(self._scan_documents, self._source_id(source))

    def _scan_documents(self, source_id: str) -> list[ConnectedDocument]:
        root = self._document_root(source_id)
        if not root.is_dir():
            return []
        documents: list[ConnectedDocument] = []
        for path in sorted(root.rglob("*.md")):
            if not self._is_document_file(root, path):
                continue
            try:
                metadata = read_frontmatter(path)
            except ValueError:
                continue
            if metadata is None or metadata.get("source") != source_id:
                continue
            if metadata.get("type") != "ConnectedDocument":
                continue
            object_id = metadata.get("external_id")
            version = metadata.get("version")
            if not isinstance(object_id, str) or not isinstance(version, str):
                continue
            documents.append(
                ConnectedDocument(
                    source_id=source_id,
                    object_id=object_id,
                    version=version,
                    classification=str(metadata.get("classification", "")),
                    content_hash=str(metadata.get("content_hash", "")),
                    path=path.relative_to(self._workspace).as_posix(),
                )
            )
        return documents

    async def get_document(
        self, source: ConnectedSource, object_id: str
    ) -> ConnectedDocument | None:
        """Get one inventory row by source object id, never returning the body."""
        documents = await self.list_documents(source)
        return next((document for document in documents if document.object_id == object_id), None)

    async def document_status(self, source: ConnectedSource, object_id: str) -> DocumentStatus:
        """Return indexed/missing state without exposing a document body."""
        return (
            DocumentStatus.INDEXED
            if await self.get_document(source, object_id) is not None
            else DocumentStatus.MISSING
        )

    async def delete_document(self, source: ConnectedSource, object_id: str) -> DocumentStatus:
        """Remove one extracted document's index/file while preserving other destinations."""
        self._require_writable()
        source_id = self._source_id(source)
        document = await self.get_document(source, object_id)
        await self._doc_index().delete_object(source_id, self._agent_did, object_id)
        if document is not None:
            await asyncio.to_thread(
                _remove_document_file,
                self._workspace / document.path,
                self._document_root(source_id),
            )
        state = await self._object_state.get_object_state(source_id, object_id)
        if state is not None:
            await self._object_state.put_object_state(
                source_id,
                object_id,
                state.model_copy(update={"path": ""}),
            )
        await self.finish_sync(source)
        return DocumentStatus.MISSING

    async def reindex_document(self, source: ConnectedSource, object_id: str) -> bool:
        """Rebuild exactly one document's chunks from its canonical extracted text."""
        self._require_writable()
        document = await self.get_document(source, object_id)
        if document is None:
            return False
        reindexed = await self._reindex_one(self._source_id(source), document)
        await self.finish_sync(source)
        return reindexed

    async def reindex_source(self, source: ConnectedSource) -> int:
        """Reindex all canonical extracted documents for a source; return successes.

        The inventory is listed once and the routing index refreshed once:
        going through ``reindex_document`` per document re-listed every
        document for each one.
        """
        self._require_writable()
        source_id = self._source_id(source)
        outcomes = [
            await self._reindex_one(source_id, document)
            for document in await self.list_documents(source)
        ]
        await self.finish_sync(source)
        return sum(outcomes)

    async def relayout_source(self, source: ConnectedSource) -> RelayoutReport:
        """Move existing documents to their mirrored source paths, without re-embedding.

        Chunk ids are keyed by the object, so a moved file keeps every chunk,
        text and vector; only the stored path pointer and the object-state path
        change. Each step is idempotent (move, repoint chunks, repoint state) and
        always runs for every document, so a crashed pass resumes by running
        again and a finished tree reports all zeros. The routing-index chunk is
        not re-embedded here; the next sync's ``finish_sync`` refreshes it.
        """
        self._require_writable()
        await self._require_current_generation(source)
        source_id = self._source_id(source)
        moves = await asyncio.to_thread(self._plan_relayout, source_id)
        moved = await asyncio.to_thread(
            self._move_documents, moves, self._document_root(source_id)
        )
        index = self._doc_index()
        repathed = state_updated = 0
        for position, move in enumerate(moves):
            pointer = move.target.relative_to(self._workspace).as_posix()
            repathed += int(
                await index.repath_object(source_id, self._agent_did, move.object_id, pointer) > 0
            )
            state = await self._object_state.get_object_state(source_id, move.object_id)
            if state is not None and not state.deleted and state.path != move.target.as_posix():
                await self._object_state.put_object_state(
                    source_id,
                    move.object_id,
                    state.model_copy(update={"path": move.target.as_posix()}),
                )
                state_updated += 1
            if position % 100 == 99:
                await asyncio.sleep(0)  # a large source must not hold the loop
        # A relayout is not a change to the knowledge: its indexes settle unlogged.
        await asyncio.to_thread(
            source_maintainer(self._document_root(source_id)).sync_all, log=False
        )
        report = RelayoutReport(
            scanned=len(moves),
            moved=moved,
            repathed=repathed,
            state_updated=state_updated,
            refused=sum(1 for move in moves if move.refused),
        )
        self._audit_relayout(source_id, report)
        return report

    def _plan_relayout(self, source_id: str) -> list[_Move]:
        """Every document's current and mirrored path, collisions resolved in order."""
        root = self._document_root(source_id)
        if not root.is_dir():
            return []
        claimed: dict[Path, str] = {}
        moves: list[_Move] = []
        for path in sorted(root.rglob("*.md")):
            if not self._is_document_file(root, path):
                continue
            try:
                metadata = read_frontmatter(path) or {}
            except ValueError:
                continue
            object_id = metadata.get("external_id")
            if metadata.get("type") != "ConnectedDocument" or not isinstance(object_id, str):
                continue
            target, refused = target_path(
                root, object_id, str(metadata.get("locator", "")), current=path
            )
            if claimed.get(target, object_id) != object_id:
                target = root / flat_name(object_id)
            claimed[target] = object_id
            moves.append(_Move(object_id=object_id, current=path, target=target, refused=refused))
        return moves

    @staticmethod
    def _move_documents(moves: list[_Move], root: Path) -> int:
        """Rename each misplaced document (atomic per file) and prune emptied folders."""
        moved = 0
        for move in moves:
            if move.current == move.target:
                continue
            move.target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(move.current, move.target)
            prune_empty_dirs(move.current.parent, root)
            moved += 1
        return moved

    def _audit_relayout(self, source_id: str, report: RelayoutReport) -> None:
        if self._audit is None:
            return
        emit(
            AuditEvent(
                actor_did=self._agent_did,
                action="connected_data.relayout",
                target=source_id,
                outcome="allow",
                payload_hash=content_hash(source_id),
                extra={key: str(value) for key, value in report.model_dump().items()},
            ),
            self._audit,
        )

    async def _reindex_one(self, source_id: str, document: ConnectedDocument) -> bool:
        path = self._workspace / document.path
        try:
            text = await asyncio.to_thread(path.read_text, encoding="utf-8")
            metadata, body = parse_document(text)
        except (OSError, UnicodeDecodeError, ValueError):
            return False
        source_object = ConnectedObject(
            object_id=document.object_id,
            locator=path.as_posix(),
            version=document.version,
            classification=str(metadata.get("classification", "")),
        )
        chunks = await self._document_chunks(source_id, source_object, body, path)
        index = self._doc_index()
        await index.delete_object(source_id, self._agent_did, document.object_id)
        await index.index_source(source_id, self._agent_did, chunks)
        return True

    def blob_folders(self, source: ConnectedSource) -> list[str]:
        """Return source-owned blob ontology entity ids for operator/agent orientation."""
        source_id = self._source_id(source)
        store = SemanticStore(self._workspace, WeightedGraph(self._db), self._agent_did)
        return [slug for slug in store.slugs() if slug.startswith(f"blob-{source_id}-")]

    async def _mapping_is_approved(self, mapping: ApprovedMapping) -> bool:
        if self._authority is not None:
            # Asked per write, never cached: the moment the last subscriber's
            # approval is gone, the next object is refused.
            grant = await self._authority.authorized_homes()
            return (
                grant is not None
                and grant[0] == mapping.mapping_id
                and set(mapping.homes) <= set(grant[1])
            )
        if self._approval is None:
            return False
        target = mapping_call_hash(
            mapping.source_id,
            mapping.homes,
            revision=mapping.revision,
            content_hash_value=mapping.content_hash,
        )
        # Verified once per run: a mapping already confirmed approved this run is
        # not re-consulted per object. This narrows the window in which two
        # objects in the same run could disagree, and cuts N approval-spine round
        # trips to one. The cache holds only positive verdicts, keyed by call_hash.
        if target in self._approved_call_hashes:
            return True
        await self._approval.start()
        # An approved mapping is durable — expiry gates only the pending window
        # (see require_approved_mapping), so an aged-out grant still authorizes ingest.
        for row in await self._approval.list(status="approved"):
            if row.id == mapping.mapping_id and row.call_hash == target:
                self._approved_call_hashes.add(target)
                return True
        return False

    async def document_search(
        self,
        query: str,
        source: ConnectedSource,
        *,
        clearance: str = "unclassified",
        top_k: int = 10,
    ) -> list[DocHit]:
        """Search exactly one approved source's document pool."""
        return await self.search_pool(
            query, self._source_id(source), clearance=clearance, top_k=top_k
        )

    async def search_pool(
        self,
        query: str,
        source_id: str,
        *,
        clearance: str = "unclassified",
        top_k: int | None = None,
    ) -> list[DocHit]:
        """Search one pool by its source id, dropping every hit above ``clearance``.

        By id, because a reader of a connection-scoped store (P18-4) holds the
        pool's id from its subscription, not the provider's description of it.
        ``top_k`` falls back to the operator's ``doc_search_top_k``.
        """
        from arctrust.classification import dominates, parse_classification

        hits = await self._doc_index().document_search(
            query, self._agent_did, source_id=source_id, top_k=top_k
        )
        strict = self._config.tier == "federal"
        caller = parse_classification(clearance, strict=strict)
        kept: list[DocHit] = []
        for hit in hits:
            try:
                label = parse_classification(hit.classification, strict=strict)
            except ValueError:
                continue
            if dominates(caller, label):
                kept.append(hit)
        return kept

    async def list_pool(self, source_id: str, *, limit: int = 50) -> list[DocHit]:
        """The documents one pool holds, newest first, without a query."""
        return await self._doc_index().list_documents(
            self._agent_did, source_id=source_id, limit=limit
        )

    def _document_target(
        self,
        source_id: str,
        source_object: ConnectedObject,
        prior: ConnectedObjectState | None,
    ) -> Path:
        """Where this object's extracted document lives, mirroring its source path.

        A locator is remote-controlled: a traversal-shaped one is refused (and
        audited) and the document keeps the flat per-object name instead.
        """
        current = Path(prior.path) if prior is not None and prior.path else None
        path, refused = target_path(
            self._document_root(source_id),
            source_object.object_id,
            source_object.locator,
            current=current,
        )
        if refused:
            self._audit_object(source_id, source_object, "layout_refused", "locator_traversal")
        return path

    @staticmethod
    def _is_document_file(root: Path, path: Path) -> bool:
        """A concept document of the pool: not a reserved file, not in a hidden folder."""
        parts = path.relative_to(root).parts
        if not (listable_file(parts[-1]) and all(listable_dir(part) for part in parts[:-1])):
            return False
        walked = root
        for part in parts:  # a planted symlink must never alias another folder into the pool
            walked = walked / part
            if walked.is_symlink():
                return False
        return True

    def _document_root(self, source_id: str) -> Path:
        """Canonical extracted-document root for one source instance."""
        return self._workspace / "memory" / "connected" / source_id

    def _remove_file(self, source_id: str, state: ConnectedObjectState | None) -> None:
        if state is not None and state.path:
            _remove_document_file(Path(state.path), self._document_root(source_id))

    def _audit_object(
        self,
        source_id: str,
        source_object: ConnectedObject,
        outcome: str,
        reason: str = "",
    ) -> None:
        if self._audit is None:
            return
        payload = content_hash(
            "\0".join((source_id, source_object.object_id, source_object.version))
        )
        emit(
            AuditEvent(
                actor_did=self._agent_did,
                action=f"connected_data.object.{outcome}",
                target=source_id,
                outcome="deny" if outcome in {"skipped", "layout_refused"} else "allow",
                payload_hash=payload,
                extra={"reason": reason} if reason else {},
            ),
            self._audit,
        )


__all__ = [
    "AdoptionReport",
    "ApprovedMapping",
    "ConnectedDataService",
    "ConnectedDocument",
    "ConnectedObject",
    "ConnectedObjectError",
    "ConnectedObjectOrderError",
    "ConnectedObjectState",
    "ConnectedObjectStatePort",
    "ConnectedObjectTooLargeError",
    "ConnectedSource",
    "ConnectedSourceShape",
    "DocumentStatus",
    "InMemoryObjectState",
    "MappingAuthority",
    "RelayoutReport",
    "SourceContent",
    "SourceMappingDeniedError",
    "SourceMappingPendingError",
    "source_instance_id",
]
