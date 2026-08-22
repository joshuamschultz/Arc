"""Database-backed, fenced ownership for the ArcFlow runner.

The lease uses only ArcStore's atomic mutable-plane primitives.  The durable
row supplies cross-process coordination; the monotonic token makes a delayed
former owner distinguishable from the current owner.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from arcstore.backends.base import ArcStoreBackend

_COLLECTION = "workflow_runner_leases"
_KEY = "default"
_SYSTEM_ACTOR = "did:arc:system:workflow-runner"


@dataclass(frozen=True)
class RunnerFence:
    """The authority returned to one lease holder."""

    owner_id: str
    token: int
    expires_at: datetime


class WorkflowRunnerLease:
    """Acquire, renew, and release the singleton runner lease with fencing."""

    def __init__(
        self,
        backend: ArcStoreBackend,
        *,
        owner_id: str | None = None,
        ttl: timedelta = timedelta(seconds=60),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if ttl <= timedelta(0):
            raise ValueError("workflow runner lease ttl must be positive")
        self._backend = backend
        self._owner_id = owner_id or f"workflow-runner:{uuid4().hex}"
        self._ttl = ttl
        self._clock = clock or (lambda: datetime.now(UTC))
        self._fence: RunnerFence | None = None

    @property
    def fence(self) -> RunnerFence | None:
        """The most recently granted fence, if this process owns the lease."""
        return self._fence

    async def acquire_or_renew(self) -> RunnerFence | None:
        """Return this owner's current fence, or ``None`` when another owner is live."""
        await self._backend.mutable_create_batch(
            _COLLECTION,
            [(_KEY, {"owner_id": None, "fencing_token": 0, "expires_at": None})],
            actor_did=_SYSTEM_ACTOR,
        )
        for _ in range(8):
            row = await self._backend.mutable_read(_COLLECTION, _KEY)
            if row is None:  # pragma: no cover - create/read is a backend invariant
                raise RuntimeError("workflow runner lease row disappeared")
            owner = row.get("owner_id")
            token = int(row.get("fencing_token", 0))
            now = _utc(self._clock())
            expires_at = _parse_expiry(row.get("expires_at"))
            if owner != self._owner_id and expires_at is not None and expires_at > now:
                self._fence = None
                return None
            next_token = token if owner == self._owner_id else token + 1
            next_expiry = now + self._ttl
            patch = {
                "owner_id": self._owner_id,
                "fencing_token": next_token,
                "expires_at": next_expiry.isoformat(),
            }
            if await self._backend.update_if(
                _COLLECTION,
                _KEY,
                patch,
                {"owner_id": owner, "fencing_token": token, "expires_at": row.get("expires_at")},
                actor_did=_SYSTEM_ACTOR,
            ):
                self._fence = RunnerFence(self._owner_id, next_token, next_expiry)
                return self._fence
        raise RuntimeError("workflow runner lease CAS contention did not settle")

    async def is_current(self, fence: RunnerFence | None = None) -> bool:
        """Check that ``fence`` still names this owner and has not expired."""
        candidate = fence or self._fence
        if candidate is None:
            return False
        row = await self._backend.mutable_read(_COLLECTION, _KEY)
        if row is None:
            return False
        expires_at = _parse_expiry(row.get("expires_at"))
        return (
            row.get("owner_id") == candidate.owner_id
            and int(row.get("fencing_token", -1)) == candidate.token
            and expires_at is not None
            and expires_at > _utc(self._clock())
        )

    async def release(self, *, fence: RunnerFence | None = None) -> bool:
        """Release only the exact token this process owns; stale owners cannot clear it."""
        candidate = fence or self._fence
        if candidate is None:
            return False
        released = await self._backend.update_if(
            _COLLECTION,
            _KEY,
            {"owner_id": None, "expires_at": _utc(self._clock()).isoformat()},
            {"owner_id": candidate.owner_id, "fencing_token": candidate.token},
            actor_did=_SYSTEM_ACTOR,
        )
        if released and candidate == self._fence:
            self._fence = None
        return released


def _utc(value: datetime) -> datetime:
    """Normalize the injected clock at the persistence boundary."""
    if value.tzinfo is None:
        raise ValueError("workflow runner lease clock must return an aware datetime")
    return value.astimezone(UTC)


def _parse_expiry(value: Any) -> datetime | None:
    """Decode a persisted ISO expiry, refusing corrupt authority state."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise RuntimeError("workflow runner lease expiry is malformed")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("workflow runner lease expiry is malformed") from exc
    return _utc(parsed)


__all__ = ["RunnerFence", "WorkflowRunnerLease"]
