"""Coordinator-only contracts layered on the canonical source seam."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from arcagent.extension.source import (
    FetchSourceObject,
    ListSourceResources,
    SelectSourceResources,
    SourceAdapter,
    SourceContent,
    SourceDescription,
    SourceError,
    SourceFailureCode,
    SourceObject,
    SourceObjectKind,
    SourceResource,
    SyncSource,
    SyncSourcePage,
)

SourceDescriptor = SourceDescription
SourcePage = SyncSourcePage


class SyncStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LEASE_LOST = "lease_lost"
    AWAITING_MAPPING = "awaiting_mapping"


class KnowledgeHome(StrEnum):
    """Canonical destination selected by an operator for connected data."""

    MEMORY = "memory"
    DOCUMENT = "document"
    DATASTORE = "datastore"
    BLOB = "blob"
    PROFILE = "profile"


class MappingPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mapping_id: str = Field(min_length=1, max_length=512)
    homes: tuple[KnowledgeHome, ...] = Field(min_length=1)
    revision: str = Field(min_length=1, max_length=512)
    content_hash: str = Field(min_length=1, max_length=128)
    metadata: Mapping[str, str] = Field(default_factory=dict)


class SyncLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_pages: int = Field(default=1000, gt=0)
    max_bytes: int = Field(default=64 * 1024 * 1024, gt=0)
    max_seconds: float = Field(default=900.0, gt=0)
    max_concurrency: int = Field(default=8, gt=0)
    page_size: int = Field(default=200, gt=0, le=2_000)
    retries: int = Field(default=2, ge=0)
    retry_backoff_seconds: float = Field(default=0.25, ge=0)


class SyncState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_did: str
    source_id: str
    cursor: str | None = None
    status: SyncStatus = SyncStatus.IDLE
    pages: int = Field(default=0, ge=0)
    bytes_processed: int = Field(default=0, ge=0)
    fencing_token: int = Field(default=0, ge=0)
    error_code: str | None = None


class SyncLease(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    owner_id: str = Field(min_length=1, max_length=512)
    fencing_token: int = Field(ge=1)
    expires_at: datetime


class SyncError(Exception):
    code = "sync_error"

    def __init__(self, message: str = "connected-data synchronization failed") -> None:
        super().__init__(message[:256])
        self.public_message = message[:256]


class MappingPendingError(SyncError):
    code = "awaiting_mapping"


class MappingDeniedError(SyncError):
    code = "mapping_denied"


class TransientSyncError(SyncError):
    code = "transient"

    def __init__(
        self, message: str = "temporary source failure", *, retry_after: float = 0.0
    ) -> None:
        super().__init__(message)
        self.retry_after = max(0.0, retry_after)


class LeaseLostError(SyncError):
    code = "lease_lost"


@runtime_checkable
class IngestPort(Protocol):
    async def require_approved_mapping(self, source: SourceDescription) -> MappingPlan: ...

    async def ingest(
        self,
        source: SourceDescription,
        source_object: SourceObject,
        content: SourceContent | None,
        mapping: MappingPlan,
    ) -> None: ...


@runtime_checkable
class SyncStatePort(Protocol):
    async def get_state(self, agent_did: str, source_id: str) -> SyncState: ...

    async def acquire_lease(
        self, agent_did: str, source_id: str, owner_id: str, *, ttl_seconds: float
    ) -> SyncLease | None: ...

    async def commit_page(
        self,
        agent_did: str,
        source_id: str,
        *,
        expected_cursor: str | None,
        next_cursor: str | None,
        page_id: str,
        page_count: int,
        page_bytes: int,
        owner_id: str,
        fencing_token: int,
    ) -> bool: ...

    async def set_status(
        self,
        agent_did: str,
        source_id: str,
        status: SyncStatus,
        *,
        owner_id: str,
        fencing_token: int,
        error_code: str | None = None,
    ) -> bool: ...

    async def release_lease(
        self, agent_did: str, source_id: str, *, owner_id: str, fencing_token: int
    ) -> None: ...

    async def renew_lease(
        self,
        agent_did: str,
        source_id: str,
        *,
        owner_id: str,
        fencing_token: int,
        ttl_seconds: float,
    ) -> bool: ...

    async def reset(self, agent_did: str, source_id: str) -> bool: ...


AuditCallback = Callable[[str, Mapping[str, Any]], Awaitable[None] | None]

__all__ = [
    "AuditCallback",
    "FetchSourceObject",
    "IngestPort",
    "KnowledgeHome",
    "LeaseLostError",
    "ListSourceResources",
    "MappingDeniedError",
    "MappingPendingError",
    "MappingPlan",
    "SelectSourceResources",
    "SourceAdapter",
    "SourceContent",
    "SourceDescription",
    "SourceDescriptor",
    "SourceError",
    "SourceFailureCode",
    "SourceObject",
    "SourceObjectKind",
    "SourcePage",
    "SourceResource",
    "SyncError",
    "SyncLease",
    "SyncLimits",
    "SyncSource",
    "SyncSourcePage",
    "SyncState",
    "SyncStatePort",
    "SyncStatus",
    "TransientSyncError",
]
