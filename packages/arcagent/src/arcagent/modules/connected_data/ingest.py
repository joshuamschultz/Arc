"""Optional ArcMemory implementation of the connected-data ``IngestPort``."""

from __future__ import annotations

import hashlib
from importlib import import_module
from pathlib import Path
from typing import Any

from arcagent.connected_data import (
    IngestPort,
    KnowledgeHome,
    MappingDeniedError,
    MappingPendingError,
    MappingPlan,
)
from arcagent.extension.source import (
    SourceContent,
    SourceDescription,
    SourceObject,
)


class ConnectedDataUnavailableError(RuntimeError):
    """ArcMemory is not installed, so connected ingestion is unavailable."""


class ArcStoreObjectState:
    """Durable object-version state over ArcStore's injected mutable-plane seam."""

    _COLLECTION = "connected_data_objects"

    def __init__(self, backend: Any, *, actor_did: str) -> None:
        self._backend = backend
        self._actor_did = actor_did

    async def get_object_state(self, source_id: str, object_id: str) -> Any | None:
        row = await self._backend.mutable_read(self._COLLECTION, self._key(source_id, object_id))
        if row is None:
            return None
        module = import_module("arcmemory.connected_data")
        return module.ConnectedObjectState.model_validate(row["state"])

    async def put_object_state(self, source_id: str, object_id: str, state: Any) -> None:
        await self._backend.mutable_write(
            self._COLLECTION,
            self._key(source_id, object_id),
            {"state": state.model_dump(mode="json")},
            actor_did=self._actor_did,
        )

    @staticmethod
    def _key(source_id: str, object_id: str) -> str:
        return hashlib.sha256(f"{source_id}\0{object_id}".encode()).hexdigest()


class ArcStoreResourceSelection:
    """Durable selected source resources, keyed without exposing source locators."""

    _COLLECTION = "connected_data_resources"

    def __init__(self, backend: Any, *, actor_did: str) -> None:
        self._backend = backend
        self._actor_did = actor_did

    async def get(self, connection_id: str) -> tuple[str, ...]:
        row = await self._backend.mutable_read(self._COLLECTION, self._key(connection_id))
        if row is None:
            return ()
        values = row.get("resource_ids", [])
        return tuple(str(value) for value in values if isinstance(value, str))

    async def put(self, connection_id: str, resource_ids: tuple[str, ...]) -> None:
        await self._backend.mutable_write(
            self._COLLECTION,
            self._key(connection_id),
            {"resource_ids": list(resource_ids)},
            actor_did=self._actor_did,
        )

    @staticmethod
    def _key(connection_id: str) -> str:
        return hashlib.sha256(connection_id.encode()).hexdigest()


class ArcMemoryIngestAdapter(IngestPort):
    """Translate the canonical source seam to optional ArcMemory primitives."""

    def __init__(
        self,
        workspace: Path,
        agent_did: str,
        *,
        approval_store: Any | None,
        object_state: Any | None = None,
        config: Any | None = None,
        embedder: Any | None = None,
        audit_sink: Any | None = None,
    ) -> None:
        self._workspace = Path(workspace)
        self._agent_did = agent_did
        self._approval_store = approval_store
        self._object_state = object_state
        self._config = config
        self._embedder = embedder
        self._audit_sink = audit_sink
        self._service: Any | None = None

    def _connected_service(self) -> Any:
        if self._service is not None:
            return self._service
        try:
            module = import_module("arcmemory.connected_data")
        except ImportError as exc:
            raise ConnectedDataUnavailableError(
                "connected ingestion requires the arcagent[memory] extra"
            ) from exc
        self._service = module.ConnectedDataService(
            self._workspace,
            self._agent_did,
            approval_store=self._approval_store,
            object_state=self._object_state,
            config=self._config,
            embedder=self._embedder,
            audit_sink=self._audit_sink,
        )
        return self._service

    @staticmethod
    def _source_model(module: Any, source: SourceDescription) -> Any:
        return module.ConnectedSource(
            connection_id=source.connection_id,
            account_id=source.account_id,
            source_kind=source.source_kind,
            supports_incremental=source.supports_incremental,
            supports_deletes=source.supports_deletes,
        )

    async def require_approved_mapping(self, source: SourceDescription) -> MappingPlan:
        """Return only an exact approved mapping; pending/denied fail closed."""
        service = self._connected_service()
        module = import_module("arcmemory.connected_data")
        try:
            mapping = await service.require_approved_mapping(self._source_model(module, source))
        except module.SourceMappingPendingError as exc:
            raise MappingPendingError() from exc
        except module.SourceMappingDeniedError as exc:
            raise MappingDeniedError() from exc
        return MappingPlan(
            mapping_id=mapping.mapping_id,
            homes=tuple(KnowledgeHome(home) for home in mapping.homes),
            revision=mapping.revision,
            content_hash=mapping.content_hash,
        )

    async def stage_mapping(
        self, source: SourceDescription, homes: tuple[KnowledgeHome, ...]
    ) -> str:
        """Stage a source-compatible mapping through the shared approval store."""
        service = self._connected_service()
        module = import_module("arcmemory.connected_data")
        try:
            return str(
                await service.propose_mapping(
                    self._source_model(module, source), tuple(home.value for home in homes)
                )
            )
        except module.SourceMappingPendingError as exc:
            raise MappingPendingError() from exc
        except module.SourceMappingDeniedError as exc:
            raise MappingDeniedError() from exc

    def allowed_homes(self, source: SourceDescription) -> tuple[KnowledgeHome, ...]:
        """Expose ArcMemory's canonical routing choices without vendor coupling."""
        service = self._connected_service()
        module = import_module("arcmemory.connected_data")
        source_model = self._source_model(module, source)
        return tuple(KnowledgeHome(home) for home in service.allowed_homes(source_model))

    def canonical_source_id(self, source: SourceDescription) -> str:
        """Return ArcMemory's stable per-agent source identity for retrieval."""
        module = import_module("arcmemory.connected_data")
        return str(module.source_instance_id(self._agent_did, self._source_model(module, source)))

    async def mapping_approval_status(self, approval_id: str) -> str:
        """Read the generic approval state without accepting caller-supplied authority."""
        if self._approval_store is None:
            return "unavailable"
        row = await self._approval_store.get(approval_id)
        return "missing" if row is None else str(row.status)

    async def list_review_items(
        self, *, status: str | None = None, source_id: str | None = None
    ) -> list[Any]:
        """Expose ArcMemory's reviewed-profile seam without leaking its implementation."""
        del source_id
        module = import_module("arcmemory.profile")
        review_status = None if status is None else module.ReviewStatus(status)
        return list(await self._connected_service().review_port.list(status=review_status))

    async def resolve_review(self, review_id: str, decision: str) -> Any | None:
        review = self._connected_service().review_port
        if decision == "approve":
            return await review.approve(review_id)
        if decision == "decline":
            return await review.decline(review_id)
        if decision == "undo":
            return await review.undo(review_id)
        raise ValueError("invalid review decision")

    async def profile_context(self, profile_id: str, *, clearance: str = "unclassified") -> Any:
        """Return only operator-approved profile facts through the review port."""
        return await self._connected_service().review_port.context(profile_id, clearance=clearance)

    async def profile_recall(
        self, profile_id: str, query: str, *, clearance: str = "unclassified"
    ) -> list[Any]:
        """Recall only approved profile facts; pending proposals cannot surface."""
        return list(
            await self._connected_service().review_port.recall(
                profile_id, query, clearance=clearance
            )
        )

    async def register_datastore(
        self, source: SourceDescription, adapter: Any, mapping: MappingPlan
    ) -> None:
        """Attach an approved source-owned datastore port to the active Brain.

        ArcAgent never imports an ArcMemory type here.  A structural source adapter
        supplies ``datastore_port`` and the selected Brain supplies
        ``register_datastore``; either missing seam is an unavailable capability,
        never a fallback that copies database rows into agent memory.
        """
        if KnowledgeHome.DATASTORE not in mapping.homes:
            return
        get_port = getattr(adapter, "datastore_port", None)
        if not callable(get_port):
            raise MappingDeniedError("selected datastore mapping has no datastore port")
        runtime = import_module("arcagent.modules.memory._runtime")
        brain = runtime.state().brain
        register = getattr(brain, "register_datastore", None)
        if not callable(register):
            raise ConnectedDataUnavailableError("active memory backend has no datastore port")
        datastore = await get_port()
        source_id = self.canonical_source_id(source)
        await register(source_id, datastore, caller_did=self._agent_did)

    async def ingest(
        self,
        source: SourceDescription,
        source_object: SourceObject,
        content: SourceContent | None,
        mapping: MappingPlan,
    ) -> None:
        """Persist one object only after the coordinator's mapping gate succeeds."""
        service = self._connected_service()
        module = import_module("arcmemory.connected_data")
        source_model = self._source_model(module, source)
        object_model = module.ConnectedObject(
            object_id=source_object.object_id,
            locator=source_object.locator,
            version=source_object.version or "deleted",
            media_type=source_object.media_type or "",
            kind=source_object.kind.value,
            deleted=source_object.deleted,
            classification=str(source_object.metadata.get("classification", "")),
            revision=_revision(source_object.metadata.get("revision")),
            metadata={key: str(value) for key, value in source_object.metadata.items()},
        )
        content_model = (
            None
            if content is None
            else module.SourceContent(
                object_id=content.object_id,
                version=content.version,
                media_type=content.media_type,
                content=content.content,
            )
        )
        mapping_model = module.ApprovedMapping(
            mapping_id=mapping.mapping_id,
            source_id=module.source_instance_id(self._agent_did, source_model),
            homes=list(mapping.homes),
            revision=mapping.revision,
            content_hash=mapping.content_hash,
        )
        await service.ingest(source_model, object_model, content_model, mapping_model)


def _revision(value: object) -> int | None:
    """Parse an optional source-provided monotonic sequence, never guess one."""
    try:
        return int(str(value)) if value is not None else None
    except ValueError:
        return None


__all__ = [
    "ArcMemoryIngestAdapter",
    "ArcStoreObjectState",
    "ArcStoreResourceSelection",
    "ConnectedDataUnavailableError",
]
