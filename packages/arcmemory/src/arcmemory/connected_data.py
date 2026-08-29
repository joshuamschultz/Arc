"""Fail-closed connected-source mapping and document ingestion."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from arcstore.approvals import ApprovalStore
from arctrust.audit import AuditEvent, AuditSink, emit
from arctrust.classification import parse_classification
from pydantic import BaseModel, ConfigDict, Field

from arcmemory.blob_ontology import BlobObject, walk_blob_source
from arcmemory.chunk import RecursiveChunker
from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB
from arcmemory.doc_index import DocHit, DocIndex
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
from arcmemory.stores.episodic import EpisodicStore
from arcmemory.stores.provenance import ProvenanceStore
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import MemoryHome, Provenance, Scope, SourceMapping, SourceRecord


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
    ) -> None:
        self._workspace = Path(workspace)
        self._agent_did = agent_did
        self._approval = approval_store
        self._config = config or MemoryConfig()
        self._audit = audit_sink
        self._db = MemoryDB(self._workspace)
        self._embedder = embedder
        self._object_state = object_state or InMemoryObjectState()
        self._reviews = review_port or ProfileReviewStore(
            self._workspace, agent_did=self._agent_did, audit_sink=self._audit
        )

    def _source_id(self, source: ConnectedSource) -> str:
        return source_instance_id(self._agent_did, source)

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
        await self._require_current_generation(source)
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
        await self._approval.start()
        now = datetime.now(UTC)
        for row in await self._approval.list():
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
            if row.call_hash != target:
                continue
            if row.status == "denied" or row.status == "expired":
                continue
            # ``expires_at`` bounds only the *pending* window (an unacted request
            # auto-cancels). An operator-approved mapping is durable: it lapses
            # only when the mapping structure changes, which re-derives a new
            # ``call_hash`` above and re-triggers approval — never on a timer.
            if row.status != "approved":
                if row.expires_at is not None and datetime.fromisoformat(row.expires_at) <= now:
                    continue
            if row.status == "approved":
                commit_mapping(proposal, store=register)
                committed = load_committed_mapping(proposal.source_id, store=register)
                if committed is None or committed.revision != proposal.revision:
                    raise SourceMappingDeniedError()
                return ApprovedMapping(
                    mapping_id=row.id,
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

    async def ingest(
        self,
        source: ConnectedSource,
        source_object: ConnectedObject,
        content: SourceContent | None,
        mapping: ApprovedMapping,
    ) -> None:
        """Safely replace one object version after verifying its exact mapping."""
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
        index = DocIndex(
            self._db,
            self._workspace,
            self._config,
            embedder=self._embedder,
            audit_sink=self._audit,
        )
        if source_object.deleted:
            # Delete all destination state, not merely routes in the newest mapping:
            # an operator can legitimately remap a source between syncs.
            await index.delete_object(source_id, self._agent_did, source_object.object_id)
            self._remove_file(prior)
            root = (
                Path(prior.path).parent
                if prior is not None and prior.path
                else self._document_path(source_id, source_object.object_id).parent
            )
            if prior is not None and prior.path:
                await index.index_collection(source_id, self._agent_did, root, [])
                if not any(path.name != "index.md" for path in root.glob("*.md")):
                    await index.delete_collection_index(source_id, self._agent_did)
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
            extracted = extractor.extract(content.content, filename=source_object.locator)
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
        clean = document_sanitize(
            extracted,
            actor_did=self._agent_did,
            tier=self._config.tier,
            audit_sink=self._audit,
        )
        profile_candidate = self._profile_candidate(source_object, mapping.homes)
        digest = content_hash(clean)
        path = self._document_path(source_id, source_object.object_id)
        if MemoryHome.DOCUMENT in mapping.homes:
            await self._write_and_index_document(
                index, source_id, source_object, clean, digest, path, prior
            )
        else:
            await index.delete_object(source_id, self._agent_did, source_object.object_id)
            self._remove_file(prior)
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
            await self._record_blob_object(source_id, source_object)
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

    async def reset_source(self, source: ConnectedSource) -> None:
        """Clear a source snapshot while preserving its approved mapping."""
        await self._clear_source(source, remove_mapping=False)

    async def complete_snapshot(
        self,
        source: ConnectedSource,
        object_ids: frozenset[str],
        mapping: ApprovedMapping,
    ) -> None:
        """Delete active objects absent from one fully successful source snapshot."""
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

    async def purge_source(self, source: ConnectedSource) -> None:
        """Irreversibly remove every retrievable artifact of a disconnected source."""
        await self._clear_source(source, remove_mapping=True)

    async def _clear_source(self, source: ConnectedSource, *, remove_mapping: bool) -> None:
        source_id = self._source_id(source)
        index = DocIndex(
            self._db,
            self._workspace,
            self._config,
            embedder=self._embedder,
            audit_sink=self._audit,
        )
        await index.delete_source(source_id, self._agent_did)
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
        source_object: ConnectedObject,
        text: str,
        digest: str,
        path: Path,
        prior: ConnectedObjectState | None,
    ) -> None:
        """Atomically persist one extracted document and replace only its chunks."""
        encoded = render_document(
            {
                "type": "ConnectedDocument",
                "source": source_id,
                "external_id": source_object.object_id,
                "version": source_object.version,
                "classification": source_object.classification,
                "content_hash": digest,
            },
            text,
        )
        atomic_write_text(path, encoded)
        if prior is not None and prior.path and prior.path != path.as_posix():
            Path(prior.path).unlink(missing_ok=True)
        chunks = self._document_chunks(source_object, text, path)
        await index.delete_object(source_id, self._agent_did, source_object.object_id)
        await index.index_collection(source_id, self._agent_did, path.parent, chunks)

    def _document_chunks(
        self, source_object: ConnectedObject, text: str, path: Path
    ) -> list[SourceChunk]:
        """Produce stable object-scoped chunks from an already-sanitized body."""
        chunker = RecursiveChunker(
            chunk_tokens=self._config.doc_chunk_tokens,
            overlap=self._config.doc_chunk_overlap,
            audit_sink=self._audit,
        )
        chunks = chunker.chunk(
            text,
            source_path=path.relative_to(self._workspace).as_posix(),
            classification=source_object.classification,
        )
        return [
            chunk.model_copy(update={"chunk_id": f"{source_object.object_id}#{position}"})
            for position, chunk in enumerate(chunks)
        ]

    async def _record_blob_object(self, source_id: str, source_object: ConnectedObject) -> None:
        """Persist one object in the source inventory and reconcile folder facts."""
        await asyncio.to_thread(self._write_blob_object, source_id, source_object)
        await asyncio.to_thread(self._reconcile_blob_inventory, source_id)

    async def _remove_blob_object(self, source_id: str, object_id: str) -> None:
        """Remove a tombstoned object before reconciling source folder ontology."""
        path = self._blob_inventory_path(source_id, object_id)
        await asyncio.to_thread(path.unlink, missing_ok=True)
        await asyncio.to_thread(self._reconcile_blob_inventory, source_id)

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
        """Return this source's indexed document inventory without exposing bodies."""
        source_id = self._source_id(source)
        root = self._document_root(source_id)
        if not root.is_dir():
            return []
        documents: list[ConnectedDocument] = []
        for path in sorted(root.glob("*.md")):
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
        source_id = self._source_id(source)
        document = await self.get_document(source, object_id)
        await DocIndex(
            self._db,
            self._workspace,
            self._config,
            embedder=self._embedder,
            audit_sink=self._audit,
        ).delete_object(source_id, self._agent_did, object_id)
        if document is not None:
            (self._workspace / document.path).unlink(missing_ok=True)
        state = await self._object_state.get_object_state(source_id, object_id)
        if state is not None:
            await self._object_state.put_object_state(
                source_id,
                object_id,
                state.model_copy(update={"path": ""}),
            )
        return DocumentStatus.MISSING

    async def reindex_document(self, source: ConnectedSource, object_id: str) -> bool:
        """Rebuild exactly one document's chunks from its canonical extracted text."""
        document = await self.get_document(source, object_id)
        if document is None:
            return False
        path = self._workspace / document.path
        try:
            metadata, body = parse_document(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            return False
        source_object = ConnectedObject(
            object_id=document.object_id,
            locator=path.as_posix(),
            version=document.version,
            classification=str(metadata.get("classification", "")),
        )
        chunks = self._document_chunks(source_object, body, path)
        index = DocIndex(
            self._db,
            self._workspace,
            self._config,
            embedder=self._embedder,
            audit_sink=self._audit,
        )
        source_id = self._source_id(source)
        await index.delete_object(source_id, self._agent_did, object_id)
        await index.index_collection(source_id, self._agent_did, path.parent, chunks)
        return True

    async def reindex_source(self, source: ConnectedSource) -> int:
        """Reindex all canonical extracted documents for a source; return successes."""
        documents = await self.list_documents(source)
        outcomes = [
            await self.reindex_document(source, document.object_id) for document in documents
        ]
        return sum(outcomes)

    def blob_folders(self, source: ConnectedSource) -> list[str]:
        """Return source-owned blob ontology entity ids for operator/agent orientation."""
        source_id = self._source_id(source)
        store = SemanticStore(self._workspace, WeightedGraph(self._db), self._agent_did)
        return [slug for slug in store.slugs() if slug.startswith(f"blob-{source_id}-")]

    async def _mapping_is_approved(self, mapping: ApprovedMapping) -> bool:
        if self._approval is None:
            return False
        await self._approval.start()
        target = mapping_call_hash(
            mapping.source_id,
            mapping.homes,
            revision=mapping.revision,
            content_hash_value=mapping.content_hash,
        )
        # An approved mapping is durable — expiry gates only the pending window
        # (see require_approved_mapping), so an aged-out grant still authorizes ingest.
        for row in await self._approval.list(status="approved"):
            if row.id == mapping.mapping_id and row.call_hash == target:
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
        from arctrust.classification import dominates, parse_classification

        index = DocIndex(
            self._db,
            self._workspace,
            self._config,
            embedder=self._embedder,
            audit_sink=self._audit,
        )
        hits = await index.document_search(
            query,
            self._agent_did,
            source_id=self._source_id(source),
            top_k=top_k,
        )
        caller = parse_classification(clearance, strict=self._config.tier == "federal")
        kept: list[DocHit] = []
        for hit in hits:
            try:
                label = parse_classification(
                    hit.classification,
                    strict=self._config.tier == "federal",
                )
            except ValueError:
                continue
            if dominates(caller, label):
                kept.append(hit)
        return kept

    def _document_path(self, source_id: str, object_id: str) -> Path:
        object_key = hashlib.sha256(object_id.encode("utf-8")).hexdigest()
        return self._document_root(source_id) / f"{object_key}.md"

    def _document_root(self, source_id: str) -> Path:
        """Canonical extracted-document root for one source instance."""
        return self._workspace / "memory" / "connected" / source_id

    @staticmethod
    def _remove_file(state: ConnectedObjectState | None) -> None:
        if state is not None and state.path:
            Path(state.path).unlink(missing_ok=True)

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
                outcome="deny" if outcome == "skipped" else "allow",
                payload_hash=payload,
                extra={"reason": reason} if reason else {},
            ),
            self._audit,
        )


__all__ = [
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
    "SourceContent",
    "SourceMappingDeniedError",
    "SourceMappingPendingError",
    "source_instance_id",
]
