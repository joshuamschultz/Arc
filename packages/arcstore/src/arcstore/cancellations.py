"""``cancellations`` domain — CancelRequest + CancelStore (operator kill switch).

The shared directory an operator surface (``arc stop`` CLI, arcui) and a live
agent meet on to stop a running agent run without SSHing into the box. Surfaces
run in *separate processes* from the agent, so an in-process handle registry can
never reach the run — the operator writes a ``pending`` row here, and a per-agent
watcher loop observes it, resolves the matching live ``RunHandle``, and calls
``RunHandle.cancel(caller_did, reason)`` (a cooperative, attributable stop —
ASI09/ASI10). The write is attributed to the operator DID (``requested_by``) so
the kill switch is auditable from request to application.

Storage is the same collection-keyed mutable plane ``TaskStore``/``ApprovalStore``
use (no new table); the race-safe ``resolve`` mirrors their ``update_if`` claim so
a request is applied exactly once even if two watcher ticks overlap.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, Literal, Protocol

from arctrust.audit import AuditSink
from pydantic import BaseModel, ConfigDict, model_validator

CancelStatus = Literal["pending", "applied", "not_found", "expired"]

_MAX_NOTE_LENGTH = 500


def _now() -> str:
    return datetime.now(UTC).isoformat()


class CancelRequest(BaseModel):
    """One operator request to stop a running agent run.

    Frozen — every transition goes through :class:`CancelStore`, which writes a
    fresh row. A run is named by ``run_id`` (arcrun's correlation id, the
    identifier the arcui timeline and traces join on) and/or ``session_key`` (the
    agent's ``_active_runs`` key); at least one must be present so the watcher can
    match a live handle. ``requested_by`` is the operator DID carried into
    ``RunHandle.cancel`` as ``caller_did`` for attribution.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    run_id: str = ""
    session_key: str = ""
    agent_label: str = ""
    requested_by: str
    reason: str = ""
    status: CancelStatus = "pending"
    note: str = ""
    resolved_by: str | None = None
    created_at: str | None = None
    resolved_at: str | None = None

    @model_validator(mode="after")
    def _require_target(self) -> CancelRequest:
        # A request with neither identifier can never match a live run — reject it
        # at construction so a malformed surface call fails loudly, not silently.
        if not self.run_id and not self.session_key:
            raise ValueError("cancel request requires a run_id or a session_key")
        return self


class MutableCancelBackend(Protocol):
    """The mutable-plane primitives :class:`CancelStore` needs (see tasks.py)."""

    async def mutable_write(
        self, collection: str, key: str, value: dict[str, Any], *,
        actor_did: str, sink: Any | None = None,
    ) -> None: ...

    async def mutable_read(self, collection: str, key: str) -> dict[str, Any] | None: ...

    async def mutable_query(
        self, collection: str, *, where: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]: ...

    async def update_if(
        self, collection: str, key: str, patch: dict[str, Any], where: dict[str, Any], *,
        actor_did: str, sink: Any | None = None, absent_where: dict[str, Any] | None = None,
    ) -> bool: ...


class CancelStore:
    """Cancel-request directory over the mutable plane's ``"cancellations"`` collection."""

    _COLLECTION: ClassVar[str] = "cancellations"

    def __init__(self, backend: MutableCancelBackend, *, sink: AuditSink | None = None) -> None:
        self._backend = backend
        self._sink = sink

    async def create(self, request: CancelRequest) -> CancelRequest:
        """Write a fresh ``pending`` cancel request, attributed to the operator."""
        request = request.model_copy(update={"created_at": _now()})
        await self._backend.mutable_write(
            self._COLLECTION,
            request.id,
            request.model_dump(mode="json"),
            actor_did=request.requested_by,
            sink=self._sink,
        )
        return request

    async def get(self, request_id: str) -> CancelRequest | None:
        raw = await self._backend.mutable_read(self._COLLECTION, request_id)
        return CancelRequest.model_validate(raw) if raw is not None else None

    async def list(self, *, status: str | None = None) -> list[CancelRequest]:
        where: dict[str, Any] = {}
        if status is not None:
            where["status"] = status
        rows = await self._backend.mutable_query(self._COLLECTION, where=where)
        items = [CancelRequest.model_validate(row) for row in rows]
        items.sort(key=lambda r: r.created_at or "", reverse=True)
        return items

    async def resolve(
        self,
        request_id: str,
        *,
        status: Literal["applied", "not_found", "expired"],
        actor_did: str,
        resolved_by: str | None = None,
        note: str = "",
    ) -> CancelRequest | None:
        """Transition a ``pending`` request to a terminal state (race-safe).

        Conditional on ``status == "pending"`` inside one atomic step so two
        overlapping watcher ticks can't both apply one request — the loser
        no-ops and returns ``None``. ``applied`` means the live run was stopped;
        ``not_found`` means no matching run existed (already ended, or a run the
        cooperative-cancel path can't reach); ``expired`` means it aged out.
        """
        patch: dict[str, Any] = {
            "status": status,
            "resolved_at": _now(),
            "resolved_by": resolved_by,
            "note": note[:_MAX_NOTE_LENGTH],
        }
        won = await self._backend.update_if(
            self._COLLECTION,
            request_id,
            patch,
            where={"status": "pending"},
            actor_did=actor_did,
            sink=self._sink,
        )
        return await self.get(request_id) if won else None

    async def expire_stale(
        self,
        *,
        ttl_seconds: int,
        actor_did: str,
        now: datetime | None = None,
    ) -> Sequence[CancelRequest]:
        """Age out pending requests older than ``ttl_seconds`` to ``expired``.

        A request whose target never materialised — the run already ended before
        the watcher saw the request, or it is a streaming run the cooperative path
        can't reach (GAP-A) — would otherwise sit ``pending`` forever. Any pending
        row whose ``created_at`` predates ``now - ttl_seconds`` is swept through the
        same conditional :meth:`resolve` claim, so a watcher tick that is
        concurrently applying the request still wins the race and cancels normally;
        the sweep loser no-ops. Returns the requests actually expired (won the
        claim) so the caller can audit each age-out.
        """
        cutoff = (now or datetime.now(UTC)) - timedelta(seconds=ttl_seconds)
        stale_ids = [
            req.id
            for req in await self.list(status="pending")
            if req.created_at and datetime.fromisoformat(req.created_at) <= cutoff
        ]
        resolved = [
            await self.resolve(
                request_id,
                status="expired",
                actor_did=actor_did,
                resolved_by=actor_did,
                note=f"aged out: no matching live run within {ttl_seconds}s TTL",
            )
            for request_id in stale_ids
        ]
        return [req for req in resolved if req is not None]


__all__ = ["CancelRequest", "CancelStatus", "CancelStore", "MutableCancelBackend"]
