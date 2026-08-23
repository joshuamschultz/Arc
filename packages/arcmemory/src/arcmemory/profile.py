"""Reviewed, provenance-bearing profile facts for connected data.

Connected content may propose a profile fact but cannot change profile context
directly.  Operators review every proposal; approved facts are the only facts
available to profile context or recall.  The store is independent of
``arcagent`` so runtime/UI may compose it through the typed :class:`ReviewPort`.
"""

from __future__ import annotations

import asyncio
import hashlib
from builtins import list as list_type
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import uuid4

from arctrust.audit import AuditEvent, AuditSink, emit
from arctrust.classification import dominates, parse_classification
from pydantic import BaseModel, ConfigDict, Field

from arcmemory.mdfile import atomic_write_text
from arcmemory.types import Provenance


class ProfileFactKind(StrEnum):
    """How a profile fact was formed; all kinds require operator approval."""

    STATIC = "static"
    DYNAMIC = "dynamic"
    INFERRED = "inferred"


class ReviewStatus(StrEnum):
    """The append-only decision state for a candidate profile fact."""

    PENDING = "pending"
    APPROVED = "approved"
    DECLINED = "declined"
    SUPERSEDED = "superseded"
    UNDONE = "undone"


class ProfileFact(BaseModel):
    """One reviewed candidate with source provenance and reversible lineage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fact_id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    field: str = Field(min_length=1)
    value: str = Field(min_length=1)
    kind: ProfileFactKind
    provenance: Provenance
    classification: str = "unclassified"
    status: ReviewStatus = ReviewStatus.PENDING
    replaces_fact_id: str | None = None


class ProfileContext(BaseModel):
    """Approved current context for one profile, split by static/dynamic facts."""

    profile_id: str
    static: dict[str, str] = Field(default_factory=dict)
    dynamic: dict[str, str] = Field(default_factory=dict)
    inferred: dict[str, str] = Field(default_factory=dict)
    facts: list_type[ProfileFact] = Field(default_factory=list)


@runtime_checkable
class ReviewPort(Protocol):
    """Public async seam composed by runtime and operated through authenticated UI."""

    async def submit(
        self,
        *,
        profile_id: str,
        field: str,
        value: str,
        kind: ProfileFactKind,
        provenance: Provenance,
        classification: str = "unclassified",
    ) -> ProfileFact: ...

    async def list(
        self, *, status: ReviewStatus | None = None, profile_id: str | None = None
    ) -> list_type[ProfileFact]: ...

    async def get(self, fact_id: str) -> ProfileFact | None: ...

    async def approve(self, fact_id: str) -> ProfileFact | None: ...

    async def decline(self, fact_id: str) -> ProfileFact | None: ...

    async def undo(self, fact_id: str) -> ProfileFact | None: ...

    async def context(
        self, profile_id: str, *, clearance: str = "unclassified"
    ) -> ProfileContext: ...

    async def recall(
        self, profile_id: str, query: str, *, clearance: str = "unclassified"
    ) -> list_type[ProfileFact]: ...


class ProfileReviewStore:
    """Workspace-local implementation of the reviewed profile-fact port."""

    def __init__(
        self,
        workspace: Path,
        *,
        agent_did: str = "",
        audit_sink: AuditSink | None = None,
    ) -> None:
        self._root = Path(workspace) / "memory" / "profile_reviews"
        self._agent_did = agent_did
        self._audit = audit_sink

    async def submit(
        self,
        *,
        profile_id: str,
        field: str,
        value: str,
        kind: ProfileFactKind,
        provenance: Provenance,
        classification: str = "unclassified",
    ) -> ProfileFact:
        """Stage a candidate without making it readable as profile context."""
        parse_classification(classification, strict=True)
        fact = ProfileFact(
            fact_id=str(uuid4()),
            profile_id=profile_id,
            field=field,
            value=value,
            kind=kind,
            provenance=provenance,
            classification=classification,
        )
        await asyncio.to_thread(self._write, fact)
        self._emit("submit", fact)
        return fact

    async def list(
        self, *, status: ReviewStatus | None = None, profile_id: str | None = None
    ) -> list_type[ProfileFact]:
        """List candidate facts, optionally narrowed by decision and profile."""
        facts = await asyncio.to_thread(self._read_all)
        matches = [
            fact
            for fact in facts
            if (status is None or fact.status == status)
            and (profile_id is None or fact.profile_id == profile_id)
        ]
        self._emit("list", target=profile_id or "", count=len(matches))
        return matches

    async def get(self, fact_id: str) -> ProfileFact | None:
        """Fetch one candidate by opaque id."""
        fact = await asyncio.to_thread(self._read, fact_id)
        self._emit("get", fact, target=fact_id)
        return fact

    async def approve(self, fact_id: str) -> ProfileFact | None:
        """Approve a pending fact and supersede the current same-field value."""
        fact = await self.get(fact_id)
        if fact is None or fact.status is not ReviewStatus.PENDING:
            return None
        prior = await self._current_for_field(fact)
        if prior is not None:
            superseded = prior.model_copy(update={"status": ReviewStatus.SUPERSEDED})
            await asyncio.to_thread(self._write, superseded)
        approved = fact.model_copy(
            update={
                "status": ReviewStatus.APPROVED,
                "replaces_fact_id": prior.fact_id if prior else None,
            }
        )
        await asyncio.to_thread(self._write, approved)
        self._emit("approve", approved)
        return approved

    async def decline(self, fact_id: str) -> ProfileFact | None:
        """Decline a pending candidate; declined content cannot be recalled."""
        fact = await self.get(fact_id)
        if fact is None or fact.status is not ReviewStatus.PENDING:
            return None
        declined = fact.model_copy(update={"status": ReviewStatus.DECLINED})
        await asyncio.to_thread(self._write, declined)
        self._emit("decline", declined)
        return declined

    async def undo(self, fact_id: str) -> ProfileFact | None:
        """Undo an approval and restore the fact it superseded, when present."""
        fact = await self.get(fact_id)
        if fact is None or fact.status is not ReviewStatus.APPROVED:
            return None
        if fact.replaces_fact_id is not None:
            replaced = await self.get(fact.replaces_fact_id)
            if replaced is not None and replaced.status is ReviewStatus.SUPERSEDED:
                await asyncio.to_thread(
                    self._write, replaced.model_copy(update={"status": ReviewStatus.APPROVED})
                )
        undone = fact.model_copy(update={"status": ReviewStatus.UNDONE})
        await asyncio.to_thread(self._write, undone)
        self._emit("undo", undone)
        return undone

    async def context(self, profile_id: str, *, clearance: str = "unclassified") -> ProfileContext:
        """Return only approved, clearance-permitted current profile facts."""
        facts = await self._approved_readable(profile_id, clearance)
        static = {fact.field: fact.value for fact in facts if fact.kind is ProfileFactKind.STATIC}
        dynamic = {
            fact.field: fact.value for fact in facts if fact.kind is ProfileFactKind.DYNAMIC
        }
        inferred = {
            fact.field: fact.value for fact in facts if fact.kind is ProfileFactKind.INFERRED
        }
        context = ProfileContext(
            profile_id=profile_id,
            static=static,
            dynamic=dynamic,
            inferred=inferred,
            facts=facts,
        )
        self._emit("context", target=profile_id, count=len(facts))
        return context

    async def recall(
        self, profile_id: str, query: str, *, clearance: str = "unclassified"
    ) -> list_type[ProfileFact]:
        """Text-match approved readable profile facts; no candidate ever surfaces."""
        needle = query.casefold()
        matches = [
            fact
            for fact in await self._approved_readable(profile_id, clearance)
            if needle in f"{fact.field} {fact.value}".casefold()
        ]
        self._emit("recall", target=profile_id, count=len(matches))
        return matches

    async def _current_for_field(self, candidate: ProfileFact) -> ProfileFact | None:
        facts = await self.list(status=ReviewStatus.APPROVED, profile_id=candidate.profile_id)
        return next((fact for fact in facts if fact.field == candidate.field), None)

    async def _approved_readable(self, profile_id: str, clearance: str) -> list_type[ProfileFact]:
        caller = parse_classification(clearance, strict=True)
        facts = await self.list(status=ReviewStatus.APPROVED, profile_id=profile_id)
        readable: list_type[ProfileFact] = []
        for fact in facts:
            resource = parse_classification(fact.classification, strict=True)
            if dominates(caller, resource):
                readable.append(fact)
        return readable

    def _path(self, fact_id: str) -> Path:
        digest = hashlib.sha256(fact_id.encode("utf-8")).hexdigest()
        return self._root / f"{digest}.json"

    def _write(self, fact: ProfileFact) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self._path(fact.fact_id), fact.model_dump_json() + "\n")

    def _read(self, fact_id: str) -> ProfileFact | None:
        path = self._path(fact_id)
        try:
            return ProfileFact.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _read_all(self) -> list_type[ProfileFact]:
        if not self._root.is_dir():
            return []
        facts: list_type[ProfileFact] = []
        for path in sorted(self._root.glob("*.json")):
            try:
                facts.append(ProfileFact.model_validate_json(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return facts

    def _emit(
        self,
        action: str,
        fact: ProfileFact | None = None,
        *,
        target: str = "",
        count: int | None = None,
    ) -> None:
        if self._audit is None or not self._agent_did:
            return
        resolved_target = target or (fact.profile_id if fact is not None else "")
        payload = fact.fact_id if fact is not None else resolved_target
        extra = {"count": count} if count is not None else {}
        emit(
            AuditEvent(
                actor_did=self._agent_did,
                action=f"memory.profile_review.{action}",
                target=resolved_target,
                outcome="allow",
                payload_hash=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                extra=extra,
            ),
            self._audit,
        )


__all__ = [
    "ProfileContext",
    "ProfileFact",
    "ProfileFactKind",
    "ProfileReviewStore",
    "ReviewPort",
    "ReviewStatus",
]
