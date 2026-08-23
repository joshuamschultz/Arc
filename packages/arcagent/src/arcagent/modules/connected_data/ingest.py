"""Optional ArcMemory implementation of the connected-data ``IngestPort``."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Any

from arcagent.connected_data import (
    IngestPort,
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
            revision=mapping.revision,
            content_hash=mapping.content_hash,
        )

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
            homes=["document"],
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


__all__ = ["ArcMemoryIngestAdapter", "ConnectedDataUnavailableError"]
