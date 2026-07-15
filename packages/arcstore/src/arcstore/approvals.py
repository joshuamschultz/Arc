"""``approvals`` domain — PendingApproval + ApprovalStore (mechanical HITL).

The shared directory an agent's ``HumanGate`` and the operator surfaces (``arc
approve`` CLI, arcui) meet on. When a trifecta-completing call is blocked, the
agent writes a ``pending`` row here; an operator resolves it out-of-band by
attaching an operator-signed ``ApprovalGrant`` (``approved``) or marking it
``denied``. The agent polls, verifies the grant against the operator's public
key, and proceeds or fails closed — approval never travels over agent chat, so a
prompt-injected or foreign message can't forge it.

Storage is the same collection-keyed mutable plane ``TaskStore`` uses (no new
table); the race-safe ``resolve`` mirrors the tasks ``update_if`` claim so two
operators can't double-resolve one request.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar, Literal, Protocol

from arctrust.audit import AuditSink
from pydantic import BaseModel, ConfigDict, Field

ApprovalStatus = Literal["pending", "approved", "denied", "expired"]

_MAX_NOTE_LENGTH = 500


def _now() -> str:
    return datetime.now(UTC).isoformat()


class PendingApproval(BaseModel):
    """One blocked trifecta-completing call awaiting an operator decision.

    Frozen — every transition goes through :class:`ApprovalStore`, which writes a
    fresh row. ``call_hash`` binds an eventual grant to exactly this call; ``grant``
    holds the operator-signed :class:`~arctrust.policy.ApprovalGrant` in wire form
    (``arctrust.policy.grant_to_wire``) once resolved ``approved``.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    agent_did: str
    agent_label: str = ""
    tool: str
    legs: list[str] = Field(default_factory=list)
    call_hash: str
    session_id: str = ""
    # SPEC-035 approval enrichment — operator triage context. ``arguments`` is a
    # redacted, length-bounded per-argument preview (WHAT is being acted on);
    # ``provenance`` records which prior tool calls lit each trifecta leg and when
    # (WHY the composition is complete). Both are already redacted/bounded by the
    # agent before the row is written — the store treats them as opaque data.
    arguments: dict[str, str] = Field(default_factory=dict)
    provenance: list[dict[str, Any]] = Field(default_factory=list)
    status: ApprovalStatus = "pending"
    note: str = ""
    grant: dict[str, Any] | None = None
    resolved_by: str | None = None
    created_at: str | None = None
    resolved_at: str | None = None
    expires_at: str | None = None


class MutableApprovalBackend(Protocol):
    """The mutable-plane primitives :class:`ApprovalStore` needs (see tasks.py)."""

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


class ApprovalStore:
    """Pending-approval directory over the mutable plane's ``"approvals"`` collection."""

    _COLLECTION: ClassVar[str] = "approvals"

    def __init__(self, backend: MutableApprovalBackend, *, sink: AuditSink | None = None) -> None:
        self._backend = backend
        self._sink = sink

    async def create(self, approval: PendingApproval) -> PendingApproval:
        approval = approval.model_copy(update={"created_at": _now()})
        await self._backend.mutable_write(
            self._COLLECTION,
            approval.id,
            approval.model_dump(mode="json"),
            actor_did=approval.agent_did,
            sink=self._sink,
        )
        return approval

    async def get(self, approval_id: str) -> PendingApproval | None:
        raw = await self._backend.mutable_read(self._COLLECTION, approval_id)
        return PendingApproval.model_validate(raw) if raw is not None else None

    async def list(self, *, status: str | None = None) -> list[PendingApproval]:
        where: dict[str, Any] = {}
        if status is not None:
            where["status"] = status
        rows = await self._backend.mutable_query(self._COLLECTION, where=where)
        items = [PendingApproval.model_validate(row) for row in rows]
        items.sort(key=lambda a: a.created_at or "", reverse=True)
        return items

    async def resolve(
        self,
        approval_id: str,
        *,
        status: Literal["approved", "denied", "expired"],
        actor_did: str,
        grant: dict[str, Any] | None = None,
        resolved_by: str | None = None,
        note: str = "",
    ) -> PendingApproval | None:
        """Transition a ``pending`` request to a terminal state (race-safe).

        Conditional on ``status == "pending"`` inside one atomic step so two
        operators (or an operator and the timeout sweep) can't both resolve one
        request — the loser no-ops and returns None. ``grant`` (wire form) is set
        only on ``approved``.
        """
        patch: dict[str, Any] = {
            "status": status,
            "resolved_at": _now(),
            "resolved_by": resolved_by,
            "note": note[:_MAX_NOTE_LENGTH],
        }
        if grant is not None:
            patch["grant"] = grant
        won = await self._backend.update_if(
            self._COLLECTION,
            approval_id,
            patch,
            where={"status": "pending"},
            actor_did=actor_did,
            sink=self._sink,
        )
        return await self.get(approval_id) if won else None


__all__ = ["ApprovalStatus", "ApprovalStore", "MutableApprovalBackend", "PendingApproval"]
