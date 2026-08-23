"""Fail-closed connected-source mapping and document ingestion."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from arcstore.approvals import ApprovalStore
from arctrust.audit import AuditEvent, AuditSink, emit
from arctrust.classification import parse_classification
from pydantic import BaseModel, ConfigDict, Field

from arcmemory.chunk import RecursiveChunker
from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB
from arcmemory.doc_index import DocHit, DocIndex
from arcmemory.extract import ExtractionUnavailable, get_extractor
from arcmemory.index.graph import WeightedGraph
from arcmemory.index.rebuild import Embedder
from arcmemory.mapping import (
    commit_mapping,
    load_committed_mapping,
    mapping_call_hash,
    stage_mapping_proposal,
)
from arcmemory.mdfile import atomic_write_text, render_document
from arcmemory.security import content_hash, document_sanitize
from arcmemory.stores.provenance import ProvenanceStore
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import Provenance, SourceMapping


class ConnectedSource(BaseModel):
    """Vendor-neutral connected-source identity and capabilities."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    connection_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    source_kind: str = Field(min_length=1)
    supports_incremental: bool = True
    supports_deletes: bool = True


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
    homes: list[str] = Field(default_factory=list)
    revision: str = Field(min_length=1)
    content_hash: str = Field(min_length=1)


class ConnectedObjectState(BaseModel):
    """Object membership supplied by the durable source-sync state owner."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    revision: int | None = None
    path: str = ""
    deleted: bool = False


class ConnectedObjectStatePort(Protocol):
    """Narrow object-state seam; production may implement it in ArcStore."""

    async def get_object_state(
        self, source_id: str, object_id: str
    ) -> ConnectedObjectState | None: ...

    async def put_object_state(
        self, source_id: str, object_id: str, state: ConnectedObjectState
    ) -> None: ...


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
    raw = "\0".join((agent_did, source.connection_id, source.account_id))
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
    ) -> None:
        self._workspace = Path(workspace)
        self._agent_did = agent_did
        self._approval = approval_store
        self._config = config or MemoryConfig()
        self._audit = audit_sink
        self._db = MemoryDB(self._workspace)
        self._embedder = embedder
        self._object_state = object_state or InMemoryObjectState()

    def _source_id(self, source: ConnectedSource) -> str:
        return source_instance_id(self._agent_did, source)

    def _proposal(self, source: ConnectedSource) -> SourceMapping:
        homes: list[str] = []
        if source.source_kind.lower() in {
            "dropbox",
            "docs",
            "document",
            "onedrive",
            "drive",
        }:
            homes.append("document")
        if source.source_kind.lower() in {"database", "postgres", "mysql", "sqlite"}:
            homes.append("datastore")
        canonical = json.dumps(
            {
                "account_id": source.account_id,
                "connection_id": source.connection_id,
                "homes": homes,
                "source_kind": source.source_kind,
                "supports_deletes": source.supports_deletes,
                "supports_incremental": source.supports_incremental,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        revision = content_hash(canonical)
        return SourceMapping(
            source_id=self._source_id(source),
            homes=homes,
            revision=revision,
            content_hash=content_hash(canonical + "\0" + revision),
        )

    async def require_approved_mapping(self, source: ConnectedSource) -> ApprovedMapping:
        """Register, stage once, and load only an exact active approval."""
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
        proposal = self._proposal(source)
        if not proposal.homes:
            raise SourceMappingDeniedError()
        if self._approval is None:
            raise SourceMappingPendingError()
        await self._approval.start()
        target = mapping_call_hash(
            proposal.source_id,
            proposal.homes,
            revision=proposal.revision,
            content_hash_value=proposal.content_hash,
        )
        matches = [row for row in await self._approval.list() if row.call_hash == target]
        now = datetime.now(UTC)
        for row in matches:
            if row.expires_at is not None and datetime.fromisoformat(row.expires_at) <= now:
                raise SourceMappingDeniedError()
            if row.status == "denied" or row.status == "expired":
                raise SourceMappingDeniedError()
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
        proposal = self._proposal(source)
        if (
            mapping.source_id != proposal.source_id
            or mapping.revision != proposal.revision
            or mapping.content_hash != proposal.content_hash
            or "document" not in mapping.homes
        ):
            raise SourceMappingDeniedError()
        if not await self._mapping_is_approved(mapping):
            raise SourceMappingDeniedError()
        source_id = self._source_id(source)
        try:
            parse_classification(source_object.classification, strict=True)
        except ValueError as exc:
            self._audit_object(source_id, source_object, "skipped", "invalid_classification")
            raise ConnectedObjectError("connected object classification is invalid") from exc
        prior = await self._object_state.get_object_state(source_id, source_object.object_id)
        if prior is not None:
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
            await index.delete_object(source_id, self._agent_did, source_object.object_id)
            self._remove_file(prior)
            root = (
                Path(prior.path).parent
                if prior is not None and prior.path
                else self._document_path(source_id, source_object.object_id).parent
            )
            if prior is not None and prior.path:
                await index.index_collection(source_id, self._agent_did, root, [])
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
        except (ValueError, OSError, RuntimeError) as exc:
            self._audit_object(source_id, source_object, "skipped", "extraction_failed")
            raise ConnectedObjectError("connected object extraction failed") from exc
        clean = document_sanitize(
            extracted,
            actor_did=self._agent_did,
            tier=self._config.tier,
            audit_sink=self._audit,
        )
        digest = content_hash(clean)
        path = self._document_path(source_id, source_object.object_id)
        encoded = render_document(
            {
                "type": "ConnectedDocument",
                "source": source_id,
                "external_id": source_object.object_id,
                "version": source_object.version,
                "classification": source_object.classification,
                "content_hash": digest,
            },
            clean,
        )
        atomic_write_text(path, encoded)
        if prior is not None and prior.path and prior.path != path.as_posix():
            Path(prior.path).unlink(missing_ok=True)
        root = path.parent
        chunker = RecursiveChunker(
            chunk_tokens=self._config.doc_chunk_tokens,
            overlap=self._config.doc_chunk_overlap,
            audit_sink=self._audit,
        )
        chunks = chunker.chunk(
            clean,
            source_path=path.relative_to(self._workspace).as_posix(),
            classification=source_object.classification,
        )
        chunks = [
            chunk.model_copy(update={"chunk_id": f"{source_object.object_id}#{index}"})
            for index, chunk in enumerate(chunks)
        ]
        await index.delete_object(source_id, self._agent_did, source_object.object_id)
        await index.index_collection(source_id, self._agent_did, root, chunks)
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
                path=path.as_posix(),
            ),
        )
        self._audit_object(source_id, source_object, "indexed")

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
        now = datetime.now(UTC)
        for row in await self._approval.list(status="approved"):
            if row.id != mapping.mapping_id or row.call_hash != target:
                continue
            if row.expires_at is None or datetime.fromisoformat(row.expires_at) > now:
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
        return self._workspace / "memory" / "connected" / source_id / f"{object_key}.md"

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
    "ConnectedObject",
    "ConnectedObjectError",
    "ConnectedObjectOrderError",
    "ConnectedObjectState",
    "ConnectedObjectStatePort",
    "ConnectedObjectTooLargeError",
    "ConnectedSource",
    "InMemoryObjectState",
    "SourceContent",
    "SourceMappingDeniedError",
    "SourceMappingPendingError",
    "source_instance_id",
]
