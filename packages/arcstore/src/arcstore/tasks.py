"""``tasks`` domain — Task model + TaskStore (SPEC-056 Phase A).

Task directory over the Phase-0A mutable plane (SDD §2): collection
``"tasks"``, one row per task, atomic claim/assign so ownership can never
race (NFR-2/G3). ``MutableTaskBackend`` is the narrow seam this module
actually needs off ``StorageBackend`` — ``base.StorageBackend`` predates the
mutable plane and doesn't yet declare it (SPEC-032 migration), so a local
Protocol keeps ``TaskStore`` structurally typed without widening the shared
contract.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from arctrust.audit import AuditSink
from pydantic import BaseModel, ConfigDict, Field, field_validator

TaskStatus = Literal["backlog", "todo", "in_progress", "review", "done", "failed"]
Priority = Literal["low", "medium", "high", "critical"]

# Claim ordering (SDD §2): highest priority first.
_PRIORITY_ORDER: dict[Priority, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3}

_MAX_TEXT_LENGTH = 2000

# Zero-width characters used in Unicode homoglyph / split-token attacks.
_ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\u200e\u200f\ufeff]")

# Prompt-injection patterns (LLM01/ASI06). Kept byte-for-byte in sync with the
# scheduler's validator so every Task construction path — agent tool, arcui,
# arccli — is sanitized identically by the model, not by a caller that might
# forget to call it (SEC-F2/ARCH-4).
_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bignore\s+previous\b", re.IGNORECASE),
    re.compile(r"\bdisregard\b", re.IGNORECASE),
    re.compile(r"\binstead\b.*\bdo\b", re.IGNORECASE),
    re.compile(r"\bsystem:", re.IGNORECASE),
    re.compile(r"\bassistant:", re.IGNORECASE),
    re.compile(r"\bexfiltrate\b", re.IGNORECASE),
    re.compile(r"https?://", re.IGNORECASE),
    re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", re.IGNORECASE),
    re.compile(r"\bforget\b.*\binstructions?\b", re.IGNORECASE),
    re.compile(r"\bnew\s+instructions?\b", re.IGNORECASE),
    re.compile(r"\boverride\b", re.IGNORECASE),
    re.compile(r"<\|[a-z_]+\|>", re.IGNORECASE),  # role delimiters like <|system|>
    re.compile(r"\bbase64\b", re.IGNORECASE),
    re.compile(r"\bdo\s+not\s+follow\b", re.IGNORECASE),
)


def _validate_free_text(text: str) -> None:
    """Reject over-length or injection-bearing free text. Raises ``ValueError``.

    NFKC folds homoglyphs (e.g. full-width Latin) to ASCII and zero-width
    separators are stripped, so neither can slip a trigger phrase past the
    regex. Used by the ``Task`` field validators — a raised ``ValueError``
    surfaces as a Pydantic ``ValidationError`` at construction.
    """
    if len(text) > _MAX_TEXT_LENGTH:
        raise ValueError(f"Text exceeds maximum length ({len(text)} > {_MAX_TEXT_LENGTH})")
    normalized = _ZERO_WIDTH_RE.sub("", unicodedata.normalize("NFKC", text))
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(normalized):
            raise ValueError("Text rejected: possible injection pattern detected")


class Task(BaseModel):
    """A unit of work in the mission-control directory (SDD §2).

    Frozen — mutation always goes through ``TaskStore``, which reads the
    durable row and writes a new one; nothing holds a live ``Task`` and edits
    it in place.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    title: str
    description: str = ""
    status: TaskStatus = "backlog"
    priority: Priority = "medium"
    owner_did: str | None = None
    creator_did: str
    parent_id: str | None = None
    run_id: str | None = None
    blocked_by: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] | None = None
    resolution: str | None = None
    # No-write-down (SEC-F3): downstream notify propagates this so a task's
    # classification bounds where it can surface. Defaults to the lowest tier.
    classification: str = "UNCLASSIFIED"
    created_at: str | None = None
    updated_at: str | None = None

    @field_validator("title", "description")
    @classmethod
    def _sanitize_free_text(cls, value: str) -> str:
        # Sanitize by construction (SEC-F2/ARCH-4): every path that builds a
        # Task — agent tool, arcui, arccli — is validated here, so a human path
        # can't bypass the injection/oversized/zero-width checks.
        _validate_free_text(value)
        return value


class MutableTaskBackend(Protocol):
    """The mutable-plane primitives ``TaskStore`` needs (SPEC-056 0a)."""

    async def mutable_write(
        self,
        collection: str,
        key: str,
        value: dict[str, Any],
        *,
        actor_did: str,
        sink: Any | None = None,
    ) -> None: ...

    async def mutable_read(self, collection: str, key: str) -> dict[str, Any] | None: ...

    async def mutable_query(
        self, collection: str, *, where: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]: ...

    async def mutable_merge(
        self,
        collection: str,
        key: str,
        patch: dict[str, Any],
        *,
        actor_did: str,
        sink: Any | None = None,
    ) -> bool: ...

    async def update_if(
        self,
        collection: str,
        key: str,
        patch: dict[str, Any],
        where: dict[str, Any],
        *,
        actor_did: str,
        sink: Any | None = None,
        absent_where: dict[str, Any] | None = None,
    ) -> bool: ...


def _is_chain_relative(active: Task, target: Task) -> bool:
    """True if ``target`` is a dependency-chain relative of ``active`` (FR-5).

    A ``blocked_by`` edge in either direction, or a shared/parent ``parent_id``,
    exempts ``target`` from the one-``in_progress``-task cap — it's the same
    piece of work, not a second independent task.
    """
    if target.id in active.blocked_by or active.id in target.blocked_by:
        return True
    if target.parent_id == active.id or active.parent_id == target.id:
        return True
    return target.parent_id is not None and target.parent_id == active.parent_id


class TaskStore:
    """Task directory over the mutable plane's ``"tasks"`` collection."""

    _COLLECTION = "tasks"

    def __init__(self, backend: MutableTaskBackend, *, sink: AuditSink | None = None) -> None:
        self._backend = backend
        self._sink = sink

    async def create(self, task: Task) -> Task:
        # A caller that didn't set status explicitly gets the SDD §4 default
        # (owned -> todo, unowned -> backlog); an explicit status is never
        # second-guessed. ``model_fields_set`` is how Pydantic distinguishes
        # "left at default" from "passed the default value on purpose".
        if "status" not in task.model_fields_set:
            derived: TaskStatus = "todo" if task.owner_did is not None else "backlog"
            task = task.model_copy(update={"status": derived})
        now = datetime.now(UTC).isoformat()
        task = task.model_copy(update={"created_at": now, "updated_at": now})
        await self._backend.mutable_write(
            self._COLLECTION,
            task.id,
            task.model_dump(mode="json"),
            actor_did=task.creator_did,
            sink=self._sink,
        )
        return task

    async def get(self, task_id: str) -> Task | None:
        raw = await self._backend.mutable_read(self._COLLECTION, task_id)
        return Task(**raw) if raw is not None else None

    async def list(
        self, *, status: str | None = None, owner_did: str | None = None
    ) -> list[Task]:
        where: dict[str, Any] = {}
        if status is not None:
            where["status"] = status
        if owner_did is not None:
            where["owner_did"] = owner_did
        rows = await self._backend.mutable_query(self._COLLECTION, where=where)
        return [Task(**row) for row in rows]

    async def update(self, task_id: str, patch: dict[str, Any], *, actor_did: str) -> Task | None:
        # Sanitize free text on the WRITE path too (SEC-F2/ARCH-4): the model's
        # field validator only fires on construction (create) and read-back —
        # a patch merges raw, so an injection title/description/resolution would
        # otherwise persist unsanitized (and a poisoned title would then brick
        # every subsequent ``Task(**raw)`` read). Validating here keeps the
        # arcstore boundary the single sanitization point for every caller.
        for key in ("title", "description", "resolution"):
            value = patch.get(key)
            if isinstance(value, str):
                _validate_free_text(value)
        # Atomic server-side merge (REL-F2): a read-merge-write here lets two
        # concurrent updates of disjoint fields clobber each other (last-writer
        # drops the loser's field). mutable_merge applies the patch inside one
        # SQLite step, so both land. False means the row is gone.
        merged = await self._backend.mutable_merge(
            self._COLLECTION, task_id, patch, actor_did=actor_did, sink=self._sink
        )
        if not merged:
            return None
        return await self.get(task_id)

    async def edit(
        self, task_id: str, patch: dict[str, Any], *, actor_did: str
    ) -> tuple[Task | None, str]:
        """Edit an at-rest task with a status-conditional write (REL-F1/SEC-F4).

        Refuses an ``in_progress`` task (edit is at-rest only, NFR-4) and makes
        the write conditional on the status not having changed since the read —
        so a task an owner races into ``in_progress`` between the check and the
        write is rejected, never silently clobbered by ``update``'s
        unconditional merge. Mirrors ``assign``'s snapshot-in-WHERE guard.

        Returns ``(task, "applied")`` on success, else ``(None, reason)`` where
        ``reason`` is ``"not_found"``, ``"in_progress"``, or ``"conflict"`` (the
        row raced out from under the read).
        """
        current = await self.get(task_id)
        if current is None:
            return None, "not_found"
        if current.status == "in_progress":
            return None, "in_progress"
        won = await self._backend.update_if(
            self._COLLECTION,
            task_id,
            patch,
            where={"status": current.status},
            actor_did=actor_did,
            sink=self._sink,
        )
        if not won:
            return None, "conflict"
        return await self.get(task_id), "applied"

    async def claim_next(self, agent_did: str) -> tuple[Task | None, str]:
        active = await self._backend.mutable_query(
            self._COLLECTION, where={"owner_did": agent_did, "status": "in_progress"}
        )
        if active:
            return Task(**active[0]), "continue_current"

        # The unowned pool is both `backlog` (freshly created, unassigned) and
        # `todo` (triaged) — a self-claim grabs from either, so the team backlog
        # is directly grabbable (no separate promotion step).
        candidates: list[Task] = []
        for status in ("backlog", "todo"):
            rows = await self._backend.mutable_query(
                self._COLLECTION, where={"status": status, "owner_did": None}
            )
            candidates.extend(Task(**row) for row in rows)
        claimable = [t for t in candidates if await self.deps_met(t)]
        claimable.sort(key=lambda t: _PRIORITY_ORDER[t.priority])

        for candidate in claimable:
            # update_if re-checks owner/status inside SQLite's own atomic
            # step, so it — not a read here followed by a write — is the gate
            # that makes two concurrent claimers resolve to exactly one
            # winner; losing just means try the next candidate. The where uses
            # the candidate's own status so a backlog and a todo task are both
            # claimable atomically. absent_where enforces the one-in_progress
            # cap in the same step (REL-F0): the active-check above and this
            # claim straddle two txns, so a second concurrent claim_next for the
            # same owner would otherwise land a second in_progress task.
            won = await self._backend.update_if(
                self._COLLECTION,
                candidate.id,
                {"owner_did": agent_did, "status": "in_progress"},
                where={"owner_did": None, "status": candidate.status},
                actor_did=agent_did,
                sink=self._sink,
                absent_where={"owner_did": agent_did, "status": "in_progress"},
            )
            if won:
                return await self.get(candidate.id), "assigned"
        return None, "no_tasks_available"

    async def start_task(self, task_id: str, agent_did: str) -> tuple[Task | None, str]:
        active_rows = await self._backend.mutable_query(
            self._COLLECTION, where={"owner_did": agent_did, "status": "in_progress"}
        )
        # Reaching the claim with an active task means this is a chain adoption
        # (an independent second task already returned continue_current below) —
        # exempt from the cap (FR-5). A claim with no active task is independent
        # and must be capped so two concurrent starts can't both land (REL-F0).
        is_chain_adoption = bool(active_rows)
        if active_rows:
            active = Task(**active_rows[0])
            target = await self.get(task_id)
            # Dependency-chain relatives are exempt from the cap (FR-5); a
            # genuinely independent second task is capped to continue_current.
            if target is None or not _is_chain_relative(active, target):
                return active, "continue_current"

        # Claim: the task is either unowned OR already assigned to this agent
        # (assign() set the owner but left it at rest) — both adopt to
        # in_progress. Snapshotting owner+status into the WHERE keeps it atomic
        # and refuses adopting a terminal or already-active task.
        target = await self.get(task_id)
        if (
            target is None
            or target.owner_did not in (None, agent_did)
            or target.status not in ("backlog", "todo")
        ):
            return None, "no_tasks_available"
        won = await self._backend.update_if(
            self._COLLECTION,
            task_id,
            {"owner_did": agent_did, "status": "in_progress"},
            where={"owner_did": target.owner_did, "status": target.status},
            actor_did=agent_did,
            sink=self._sink,
            absent_where=(
                None if is_chain_adoption else {"owner_did": agent_did, "status": "in_progress"}
            ),
        )
        if not won:
            return None, "no_tasks_available"
        return await self.get(task_id), "assigned"

    async def assign(self, task_id: str, to_did: str, by_did: str) -> Task | None:
        current = await self.get(task_id)
        if current is None or current.status not in ("backlog", "todo"):
            return None
        # Re-check the snapshotted status inside the atomic write: if the
        # task raced into in_progress between the read above and this write,
        # the WHERE no longer matches and the reassignment is rejected
        # rather than yanking active work out from under its owner (NFR-4).
        # Assigning moves an at-rest task into the owner's ready lane: owner set
        # AND status -> todo (an owned task is `todo`, per §4 create(owned)->todo),
        # so the assignee can then adopt it via start_task.
        won = await self._backend.update_if(
            self._COLLECTION,
            task_id,
            {"owner_did": to_did, "status": "todo"},
            where={"status": current.status},
            actor_did=by_did,
            sink=self._sink,
        )
        if not won:
            return None
        return await self.get(task_id)

    async def deps_met(self, task: Task) -> bool:
        for dep_id in task.blocked_by:
            dep = await self.get(dep_id)
            if dep is None or dep.status != "done":
                return False
        return True
