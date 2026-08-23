"""Durable connected-data source state with compare-and-fence semantics."""

from __future__ import annotations

import asyncio
import copy
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

SOURCE_SYNC_COLLECTION = "connected_source_sync"


class SourceSyncStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LEASE_LOST = "lease_lost"
    AWAITING_MAPPING = "awaiting_mapping"


class SourceSyncState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_did: str
    source_id: str
    cursor: str | None = None
    status: SourceSyncStatus = SourceSyncStatus.IDLE
    pages: int = Field(default=0, ge=0)
    bytes_processed: int = Field(default=0, ge=0)
    fencing_token: int = Field(default=0, ge=0)
    error_code: str | None = None


class SourceSyncLease(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    owner_id: str
    fencing_token: int = Field(ge=1)
    expires_at: datetime


class SourceSyncBackend(Protocol):
    async def source_sync_get_state(self, agent_did: str, source_id: str) -> dict[str, Any]: ...
    async def source_sync_acquire_lease(
        self, agent_did: str, source_id: str, owner_id: str, ttl_seconds: float
    ) -> dict[str, Any] | None: ...
    async def source_sync_commit_page(
        self, agent_did: str, source_id: str, **kwargs: Any
    ) -> bool: ...
    async def source_sync_set_status(
        self, agent_did: str, source_id: str, status: str, **kwargs: Any
    ) -> bool: ...
    async def source_sync_release_lease(
        self, agent_did: str, source_id: str, **kwargs: Any
    ) -> None: ...
    async def source_sync_renew_lease(
        self, agent_did: str, source_id: str, **kwargs: Any
    ) -> bool: ...
    async def source_sync_reset(self, agent_did: str, source_id: str) -> bool: ...


class InMemorySourceSyncStore:
    """Lock-protected fake implementing the source-sync persistence contract."""

    def __init__(self, *, clock: Any | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._states: dict[tuple[str, str], SourceSyncState] = {}
        self._leases: dict[tuple[str, str], SourceSyncLease] = {}
        self._pages: set[tuple[str, str, str]] = set()
        self._lock = asyncio.Lock()

    async def get_state(self, agent_did: str, source_id: str) -> SourceSyncState:
        async with self._lock:
            return copy.deepcopy(
                self._states.get(
                    (agent_did, source_id),
                    SourceSyncState(agent_did=agent_did, source_id=source_id),
                )
            )

    async def acquire_lease(
        self, agent_did: str, source_id: str, owner_id: str, *, ttl_seconds: float
    ) -> SourceSyncLease | None:
        if ttl_seconds <= 0:
            raise ValueError("lease ttl must be positive")
        key = (agent_did, source_id)
        async with self._lock:
            existing = self._leases.get(key)
            now = self._clock()
            if (
                existing is not None
                and existing.expires_at > now
                and existing.owner_id != owner_id
            ):
                return None
            state = self._states.get(
                key, SourceSyncState(agent_did=agent_did, source_id=source_id)
            )
            lease = SourceSyncLease(
                owner_id=owner_id,
                fencing_token=state.fencing_token + 1,
                expires_at=now + timedelta(seconds=ttl_seconds),
            )
            self._leases[key] = lease
            self._states[key] = state.model_copy(
                update={"fencing_token": lease.fencing_token, "status": SourceSyncStatus.RUNNING}
            )
            return lease

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
    ) -> bool:
        key = (agent_did, source_id)
        async with self._lock:
            if not self._lease_is_current(agent_did, source_id, owner_id, fencing_token):
                return False
            state = self._states[key]
            page_key = (agent_did, source_id, page_id)
            if state.cursor != expected_cursor:
                return page_key in self._pages
            if page_key in self._pages:
                return True
            self._pages.add(page_key)
            self._states[key] = state.model_copy(
                update={
                    "cursor": next_cursor,
                    "pages": state.pages + page_count,
                    "bytes_processed": state.bytes_processed + page_bytes,
                    "status": SourceSyncStatus.RUNNING,
                }
            )
            return True

    async def set_status(
        self,
        agent_did: str,
        source_id: str,
        status: str,
        *,
        owner_id: str,
        fencing_token: int,
        error_code: str | None = None,
    ) -> bool:
        async with self._lock:
            if not self._lease_is_current(agent_did, source_id, owner_id, fencing_token):
                return False
            key = (agent_did, source_id)
            self._states[key] = self._states[key].model_copy(
                update={"status": status, "error_code": error_code}
            )
            return True

    async def release_lease(
        self, agent_did: str, source_id: str, *, owner_id: str, fencing_token: int
    ) -> None:
        async with self._lock:
            key = (agent_did, source_id)
            lease = self._leases.get(key)
            if (
                lease is not None
                and lease.owner_id == owner_id
                and lease.fencing_token == fencing_token
            ):
                del self._leases[key]

    async def renew_lease(
        self,
        agent_did: str,
        source_id: str,
        *,
        owner_id: str,
        fencing_token: int,
        ttl_seconds: float,
    ) -> bool:
        if ttl_seconds <= 0:
            raise ValueError("lease ttl must be positive")
        async with self._lock:
            if not self._lease_is_current(agent_did, source_id, owner_id, fencing_token):
                return False
            key = (agent_did, source_id)
            self._leases[key] = self._leases[key].model_copy(
                update={"expires_at": self._clock() + timedelta(seconds=ttl_seconds)}
            )
            return True

    async def reset(self, agent_did: str, source_id: str) -> bool:
        """Discard a completed checkpoint only when no worker holds its lease."""
        key = (agent_did, source_id)
        async with self._lock:
            if key in self._leases and self._leases[key].expires_at > self._clock():
                return False
            state = self._states.get(key)
            if state is None:
                return True
            self._states[key] = state.model_copy(
                update={
                    "cursor": None,
                    "status": SourceSyncStatus.IDLE,
                    "pages": 0,
                    "bytes_processed": 0,
                    "error_code": None,
                }
            )
            self._pages = {
                page for page in self._pages if page[:2] != key
            }
            return True

    def _lease_is_current(
        self, agent_did: str, source_id: str, owner_id: str, fencing_token: int
    ) -> bool:
        lease = self._leases.get((agent_did, source_id))
        return (
            lease is not None
            and lease.owner_id == owner_id
            and lease.fencing_token == fencing_token
            and lease.expires_at > self._clock()
        )


class ArcStoreSourceSyncStore:
    """Adapter over an ArcStore backend's atomic source-sync primitives."""

    def __init__(self, backend: SourceSyncBackend) -> None:
        self._backend = backend

    async def get_state(self, agent_did: str, source_id: str) -> SourceSyncState:
        return SourceSyncState.model_validate(
            await self._backend.source_sync_get_state(agent_did, source_id)
        )

    async def acquire_lease(
        self, agent_did: str, source_id: str, owner_id: str, *, ttl_seconds: float
    ) -> SourceSyncLease | None:
        row = await self._backend.source_sync_acquire_lease(
            agent_did, source_id, owner_id, ttl_seconds
        )
        return None if row is None else SourceSyncLease.model_validate(row)

    async def commit_page(self, agent_did: str, source_id: str, **kwargs: Any) -> bool:
        return await self._backend.source_sync_commit_page(agent_did, source_id, **kwargs)

    async def set_status(self, agent_did: str, source_id: str, status: str, **kwargs: Any) -> bool:
        return await self._backend.source_sync_set_status(agent_did, source_id, status, **kwargs)

    async def release_lease(self, agent_did: str, source_id: str, **kwargs: Any) -> None:
        await self._backend.source_sync_release_lease(agent_did, source_id, **kwargs)

    async def renew_lease(self, agent_did: str, source_id: str, **kwargs: Any) -> bool:
        return await self._backend.source_sync_renew_lease(agent_did, source_id, **kwargs)

    async def reset(self, agent_did: str, source_id: str) -> bool:
        return await self._backend.source_sync_reset(agent_did, source_id)


__all__ = [
    "SOURCE_SYNC_COLLECTION",
    "ArcStoreSourceSyncStore",
    "InMemorySourceSyncStore",
    "SourceSyncBackend",
    "SourceSyncLease",
    "SourceSyncState",
    "SourceSyncStatus",
]
