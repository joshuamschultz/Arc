"""Optional encrypted SQLite queue journal behind CallQueueStore."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sqlite3
import stat
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, TypeVar

import arctrust

from arcllm import queue_merkle
from arcllm.exceptions import QueueFullError, QueueStateUnavailableError
from arcllm.queue_control import (
    _TERMINAL,
    CallJob,
    QueueMetadataPage,
    QueueReadScope,
    QueueRecoveryAuthority,
    QueueRecoveryPage,
    QueueState,
)

_T = TypeVar("_T")


def _key(call_id: str) -> str:
    return hashlib.sha256(call_id.encode("utf-8")).hexdigest()


class QueueJournal:
    """SQLite CAS journal with encrypted job and controller records.

    The caller owns key custody and supplies a cipher capability. This journal
    does not store a request or result, so recovery never resends a provider call.
    """

    def __init__(
        self,
        path: Path,
        cipher: arctrust.RecordCipher,
        anchor: arctrust.MonotonicAnchor,
        *,
        history_limit: int = 1000,
        recovery_authority: QueueRecoveryAuthority | None = None,
    ) -> None:
        if history_limit < 1:
            raise ValueError("history_limit must be positive")
        self._path = path
        self._cipher = cipher
        self._anchor = anchor
        self._history_limit = history_limit
        self._recovery_authority = recovery_authority
        self._broken = False
        self._lock = threading.RLock()
        self._recovery_snapshots: dict[str, tuple[tuple[tuple[str, str], ...], int, float]] = {}
        self._initialize()

    requires_recovery_owner = True

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self._lock.acquire()
        try:
            with self._open_connection() as db:
                yield db
        finally:
            self._lock.release()

    @contextmanager
    def _open_connection(self) -> Iterator[sqlite3.Connection]:
        anchor = os.open(self._path, os.O_RDWR | os.O_NOFOLLOW)
        connection: sqlite3.Connection | None = None
        try:
            opened = os.fstat(anchor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_mode & 0o077
                or opened.st_uid != os.getuid()
            ):
                raise ValueError("queue journal file is not private and regular")
            connection = sqlite3.connect(self._path, timeout=5.0)
            actual = os.stat(self._path, follow_symlinks=False)
            if (actual.st_dev, actual.st_ino) != (opened.st_dev, opened.st_ino):
                raise ValueError("queue journal path changed during open")
            connection.execute("PRAGMA busy_timeout = 5000")
            if self._broken:
                self._reconcile(connection)
            with connection:
                yield connection
        finally:
            if connection is not None:
                connection.close()
            os.close(anchor)

    def _initialize(self) -> None:
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = os.stat(self._path.parent, follow_symlinks=False)
        if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid():
            raise ValueError("queue journal parent must be an owned directory")
        if directory.st_mode & 0o077:
            raise ValueError("queue journal parent must be private")
        created = not self._path.exists()
        if not created:
            mode = os.stat(self._path, follow_symlinks=False).st_mode
            if not stat.S_ISREG(mode):
                raise ValueError("queue journal must be a regular file")
            if mode & 0o077:
                raise ValueError("queue journal permissions must exclude group and others")
        else:
            descriptor = os.open(
                self._path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600
            )
            os.close(descriptor)
        with self._connect() as db:
            if not created:
                meta = db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'queue_meta'"
                ).fetchone()
                if meta is None:
                    raise QueueStateUnavailableError(
                        "queue journal format requires operator recovery"
                    )
                version = db.execute("SELECT format FROM queue_meta WHERE id = 1").fetchone()
                if version is None or version[0] != 4:
                    raise QueueStateUnavailableError(
                        "queue journal format requires operator recovery"
                    )
            db.execute("PRAGMA journal_mode = WAL")
            if created:
                db.execute(
                    "CREATE TABLE jobs (id TEXT PRIMARY KEY, "
                    "tenant_key TEXT NOT NULL, owner_key TEXT NOT NULL, state TEXT NOT NULL, "
                    "version INTEGER NOT NULL, updated REAL NOT NULL, sealed TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE controls "
                    "(id INTEGER PRIMARY KEY CHECK(id = 1), sealed TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE queue_nodes "
                    "(prefix TEXT PRIMARY KEY, kind TEXT NOT NULL, digest TEXT NOT NULL, "
                    "children TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE queue_root "
                    "(id INTEGER PRIMARY KEY CHECK(id = 1), "
                    "prefix TEXT NOT NULL, digest TEXT NOT NULL)"
                )
                db.execute("INSERT INTO queue_root(id, prefix, digest) VALUES (1, '', '')")
                db.execute(
                    "CREATE TABLE queue_meta "
                    "(id INTEGER PRIMARY KEY CHECK(id = 1), "
                    "format INTEGER NOT NULL CHECK(format = 4))"
                )
                db.execute("INSERT INTO queue_meta(id, format) VALUES (1, 4)")
            columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
            if columns != {
                "id",
                "tenant_key",
                "owner_key",
                "state",
                "version",
                "updated",
                "sealed",
            }:
                raise QueueStateUnavailableError("queue journal format requires operator recovery")
            db.execute(
                "CREATE INDEX IF NOT EXISTS queue_jobs_page_idx ON jobs(updated DESC, id DESC)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS queue_jobs_tenant_page_idx "
                "ON jobs(tenant_key, updated DESC, id DESC)"
            )
            db.commit()
            self._reconcile(db)

    def _reconcile(self, db: sqlite3.Connection) -> None:
        local = self._digest(db)
        try:
            latest = self._anchor.latest()
            if latest is None and self._is_empty(db):
                latest = self._anchor.compare_and_advance(None, local, "")
            if latest is None or latest.scope != self._anchor.scope:
                raise QueueStateUnavailableError("queue anchor scope mismatch")
            if latest.digest != local:
                self._recover_one(db, latest, local)
            self._verify_all_leaves(db, latest.digest)
        except QueueStateUnavailableError:
            self._broken = True
            raise
        except (ValueError, TypeError, sqlite3.Error) as exc:
            self._broken = True
            raise QueueStateUnavailableError("queue journal rollback or tamper detected") from exc
        except Exception as exc:
            self._broken = True
            raise QueueStateUnavailableError("queue anchor unavailable") from exc
        self._broken = False

    @staticmethod
    def _verify_all_leaves(db: sqlite3.Connection, digest: str) -> None:
        rows = db.execute(
            "SELECT id, tenant_key, owner_key, state, version, updated, sealed FROM jobs"
        ).fetchall()
        expected = {queue_merkle.id_key(row[0]): row for row in rows}
        actual = queue_merkle.page_keys(
            db, queue_merkle.ID_SCOPE, digest, after=None, limit=len(rows) + 1
        )
        if len(actual) != len(rows) or set(actual) != set(expected):
            raise ValueError("queue ID directory omits a job")
        for key in actual:
            node = db.execute(
                "SELECT digest FROM queue_nodes WHERE prefix = ? AND kind = 'leaf'",
                (key,),
            ).fetchone()
            if node is None or node[0] != queue_merkle.row_digest(key, expected[key]):
                raise ValueError("queue ID leaf does not bind its row")
        control = db.execute("SELECT sealed FROM controls WHERE id = 1").fetchone()
        queue_merkle.verify_point(
            db,
            queue_merkle.CONTROL_KEY,
            queue_merkle.control_digest(control[0]) if control else queue_merkle.empty_leaf(),
            digest,
        )

    @staticmethod
    def _is_empty(db: sqlite3.Connection) -> bool:
        jobs: int = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        controls: int = db.execute("SELECT COUNT(*) FROM controls").fetchone()[0]
        return jobs == 0 and controls == 0

    @staticmethod
    def _digest(db: sqlite3.Connection) -> str:
        return queue_merkle.root(db)

    @staticmethod
    def _snapshot(
        db: sqlite3.Connection,
    ) -> tuple[dict[str, tuple[str, str, str, int, float, str]], str | None]:
        rows = db.execute(
            "SELECT id, tenant_key, owner_key, state, version, updated, sealed FROM jobs"
        ).fetchall()
        control = db.execute("SELECT sealed FROM controls WHERE id = 1").fetchone()
        jobs = {row[0]: tuple(row[1:]) for row in rows}
        return jobs, control[0] if control else None

    def _recover_one(self, db: sqlite3.Connection, head: arctrust.AnchorHead, local: str) -> None:
        if head.previous_digest != local or not head.intent:
            raise QueueStateUnavailableError("queue journal rollback exceeds one verified intent")
        try:
            intent = self._decode(head.intent)
            if set(intent) != {"put", "delete", "control"}:
                raise ValueError("invalid queue intent")
            db.execute("BEGIN IMMEDIATE")
            if self._digest(db) != local:
                raise ValueError("queue journal changed during recovery")
            before = self._snapshot(db)
            for key in intent["delete"]:
                db.execute("DELETE FROM jobs WHERE id = ?", (key,))
            for key, tenant_key, owner_key, state, version, updated, sealed in intent["put"]:
                self._bound_job((key, tenant_key, owner_key, state, version, updated, sealed))
                db.execute(
                    "INSERT INTO jobs(id, tenant_key, owner_key, state, version, updated, sealed) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                    "tenant_key=excluded.tenant_key, owner_key=excluded.owner_key, "
                    "state=excluded.state, version=excluded.version, "
                    "updated=excluded.updated, sealed=excluded.sealed",
                    (key, tenant_key, owner_key, state, version, updated, sealed),
                )
            if intent["control"] is not None:
                self._decode(intent["control"])
                db.execute(
                    "INSERT INTO controls(id, sealed) VALUES (1, ?) "
                    "ON CONFLICT(id) DO UPDATE SET sealed=excluded.sealed",
                    (intent["control"],),
                )
            self._update_indexes(db, before, self._snapshot(db))
            if self._digest(db) != head.digest:
                raise ValueError("replayed queue intent does not match anchor")
            db.commit()
        except Exception as exc:
            db.rollback()
            raise QueueStateUnavailableError("queue recovery intent invalid") from exc

    def _intent(
        self,
        before: tuple[dict[str, tuple[str, str, str, int, float, str]], str | None],
        after: tuple[dict[str, tuple[str, str, str, int, float, str]], str | None],
    ) -> str:
        previous_jobs, previous_control = before
        jobs, control = after
        put = [[key, *row] for key, row in jobs.items() if previous_jobs.get(key) != row]
        delete = [key for key in previous_jobs if key not in jobs]
        sealed = self._encode(
            {
                "put": put,
                "delete": delete,
                "control": control if control != previous_control else None,
            }
        )
        if len(sealed) > 1_048_576:
            raise QueueStateUnavailableError("queue recovery intent exceeds anchor limit")
        return sealed

    def _verify_anchor(
        self, db: sqlite3.Connection, *, verify_rows: bool = True
    ) -> arctrust.AnchorHead:
        try:
            head = self._anchor.latest()
            if head is None or head.scope != self._anchor.scope or head.digest != self._digest(db):
                raise QueueStateUnavailableError("queue journal diverged from anchor")
            if verify_rows:
                self._verify_all_leaves(db, head.digest)
            return head
        except (QueueStateUnavailableError, ValueError, TypeError, sqlite3.Error) as exc:
            self._broken = True
            raise QueueStateUnavailableError("queue journal rollback or tamper detected") from exc
        except Exception as exc:
            self._broken = True
            raise QueueStateUnavailableError("queue anchor unavailable") from exc

    def _commit_anchored(
        self,
        db: sqlite3.Connection,
        previous: arctrust.AnchorHead,
        before: tuple[dict[str, tuple[str, str, str, int, float, str]], str | None],
        after: tuple[dict[str, tuple[str, str, str, int, float, str]], str | None],
        recovery: tuple[str, str, str] | None = None,
    ) -> None:
        self._broken = True
        try:
            self._update_indexes(db, before, after)
            digest = self._digest(db)
            intent = self._intent(before, after)
            if recovery is None:
                head = self._anchor.compare_and_advance(previous, digest, intent)
            else:
                authority = self._recovery_authority
                if authority is None:
                    raise QueueStateUnavailableError(
                        "durable queue recovery authority unavailable"
                    )
                proof, tenant_id, owner_epoch = recovery
                head = authority.compare_and_advance(
                    previous,
                    digest,
                    intent,
                    proof,
                    journal_scope=self._anchor.scope,
                    tenant_id=tenant_id,
                    owner_epoch=owner_epoch,
                    purpose="queue.recover",
                )
            if (
                head.version != previous.version + 1
                or head.scope != previous.scope
                or head.previous_digest != previous.digest
                or head.digest != digest
                or head.intent != intent
            ):
                raise QueueStateUnavailableError("queue anchor revision did not advance")
            db.commit()
        except QueueStateUnavailableError:
            raise
        except Exception as exc:
            raise QueueStateUnavailableError("queue anchor or journal unavailable") from exc
        self._broken = False

    @staticmethod
    def _row_change(row: queue_merkle.JobRow) -> dict[str, tuple[str, str, str, int, float, str]]:
        return {row[0]: row[1:]}

    @staticmethod
    def _verify_row(
        db: sqlite3.Connection, row_id: str, row: queue_merkle.JobRow | None, digest: str
    ) -> None:
        key = queue_merkle.id_key(row_id)
        expected = queue_merkle.row_digest(key, row) if row else queue_merkle.empty_leaf()
        try:
            queue_merkle.verify_point(db, key, expected, digest)
        except (ValueError, TypeError, sqlite3.Error) as exc:
            raise QueueStateUnavailableError("queue row proof unavailable") from exc

    @staticmethod
    def _verify_control(db: sqlite3.Connection, sealed: str | None, digest: str) -> None:
        expected = queue_merkle.control_digest(sealed) if sealed else queue_merkle.empty_leaf()
        try:
            queue_merkle.verify_point(db, queue_merkle.CONTROL_KEY, expected, digest)
        except (ValueError, TypeError, sqlite3.Error) as exc:
            raise QueueStateUnavailableError("queue control rollback or tamper detected") from exc

    @staticmethod
    def _update_indexes(
        db: sqlite3.Connection,
        before: tuple[dict[str, tuple[str, str, str, int, float, str]], str | None],
        after: tuple[dict[str, tuple[str, str, str, int, float, str]], str | None],
    ) -> None:
        changes: dict[str, str] = {}
        for row_id in before[0].keys() | after[0].keys():
            old_data = before[0].get(row_id)
            new_data = after[0].get(row_id)
            if old_data == new_data:
                continue
            old_row = (row_id, *old_data) if old_data is not None else None
            new_row = (row_id, *new_data) if new_data is not None else None
            old_keys = set(queue_merkle.index_keys(old_row)) if old_row else set()
            new_keys = set(queue_merkle.index_keys(new_row)) if new_row else set()
            for key in sorted(old_keys - new_keys):
                changes[key] = queue_merkle.empty_leaf()
            if new_row is not None:
                for key in sorted(new_keys):
                    changes[key] = queue_merkle.row_digest(key, new_row)
        if before[1] != after[1]:
            digest = (
                queue_merkle.control_digest(after[1])
                if after[1] is not None
                else queue_merkle.empty_leaf()
            )
            changes[queue_merkle.CONTROL_KEY] = digest
        if changes:
            queue_merkle.set_leaves(db, changes)

    def _encode(self, value: dict[str, Any]) -> str:
        return json.dumps(self._cipher.seal({"extra": value})["extra"], separators=(",", ":"))

    def _decode(self, value: str) -> dict[str, Any]:
        sealed = json.loads(value)
        if not isinstance(sealed, dict) or set(sealed) != {"arc.audit.sealed"}:
            raise ValueError("queue journal record is not sealed")
        decoded: dict[str, Any] = self._cipher.unseal({"extra": sealed})["extra"]
        return decoded

    def _bound_job(
        self, row: tuple[str, str, str, str, int, float, str], call_id: str | None = None
    ) -> CallJob:
        key, tenant_key, owner_key, state, version, updated, sealed = row
        job = CallJob(**self._decode(sealed))
        if (
            _key(job.call_id) != key
            or queue_merkle.tenant_key(job.tenant_id) != tenant_key
            or queue_merkle.owner_key(job.owner_id) != owner_key
            or job.state != state
            or job.version != version
            or job.updated_at != updated
        ):
            raise ValueError("queue journal row binding failed")
        if call_id is not None and job.call_id != call_id:
            raise ValueError("queue journal call binding failed")
        return job

    def _create(self, job: CallJob) -> None:
        row_id = _key(job.call_id)
        queue_merkle.sort_suffix(job.updated_at, row_id)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            head = self._verify_anchor(db, verify_rows=False)
            existing = db.execute(
                "SELECT id, tenant_key, owner_key, state, version, updated, sealed "
                "FROM jobs WHERE id = ?",
                (row_id,),
            ).fetchone()
            self._verify_row(db, row_id, existing, head.digest)
            if existing is not None:
                raise ValueError("duplicate queue call ID")
            count: int = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            excess = max(0, count - self._history_limit + 1)
            evicted: dict[str, tuple[str, str, str, int, float, str]] = {}
            if excess:
                candidates = db.execute(
                    "SELECT id, tenant_key, owner_key, state, version, updated, sealed "
                    "FROM jobs WHERE state IN ('completed', 'failed', 'cancelled', "
                    "'timed_out', 'outcome_unknown') ORDER BY updated ASC, id ASC LIMIT ?",
                    (excess,),
                ).fetchall()
                if len(candidates) != excess:
                    raise QueueFullError(count, self._history_limit)
                for candidate in candidates:
                    self._verify_row(db, candidate[0], candidate, head.digest)
                    self._bound_job(candidate)
                    evicted.update(self._row_change(candidate))
                    db.execute("DELETE FROM jobs WHERE id = ?", (candidate[0],))
            row: queue_merkle.JobRow = (
                row_id,
                queue_merkle.tenant_key(job.tenant_id),
                queue_merkle.owner_key(job.owner_id),
                job.state,
                job.version,
                job.updated_at,
                self._encode(asdict(job)),
            )
            db.execute(
                "INSERT INTO jobs(id, tenant_key, owner_key, state, version, updated, sealed) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                row,
            )
            self._commit_anchored(db, head, (evicted, None), (self._row_change(row), None))

    async def create(self, job: CallJob) -> None:
        """Persist an accepted call before any provider admission."""
        await asyncio.to_thread(self._create, job)

    def _get(self, call_id: str) -> CallJob | None:
        with self._connect() as db:
            db.execute("BEGIN")
            head = self._verify_anchor(db, verify_rows=False)
            row_id = _key(call_id)
            row = db.execute(
                "SELECT id, tenant_key, owner_key, state, version, updated, sealed "
                "FROM jobs WHERE id = ?",
                (row_id,),
            ).fetchone()
            self._verify_row(db, row_id, row, head.digest)
        return self._bound_job(row, call_id) if row else None

    async def get(self, call_id: str) -> CallJob | None:
        """Read one authenticated snapshot."""
        return await self._read_retry(self._get, call_id)

    async def _read_retry(self, read: Callable[..., _T], *args: Any) -> _T:
        try:
            return await asyncio.to_thread(read, *args)
        except QueueStateUnavailableError:
            return await asyncio.to_thread(read, *args)

    def _compare_and_set(
        self,
        call_id: str,
        version: int,
        state: QueueState,
        owner_id: str,
        provider_scope: str | None,
        attempt_id: str | None,
    ) -> CallJob | None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            head = self._verify_anchor(db, verify_rows=False)
            row_id = _key(call_id)
            row = db.execute(
                "SELECT id, tenant_key, owner_key, state, version, updated, sealed "
                "FROM jobs WHERE id = ?",
                (row_id,),
            ).fetchone()
            self._verify_row(db, row_id, row, head.digest)
            if row is None or row[4] != version:
                return None
            old = self._bound_job(row, call_id)
            if (
                old.owner_id != owner_id
                or old.state in _TERMINAL
                or (old.state == "cancel_requested" and state == "running")
            ):
                return None
            new = replace(
                old,
                state=state,
                version=version + 1,
                updated_at=time.time(),
                provider_scope=provider_scope or old.provider_scope,
                attempt_id=attempt_id or old.attempt_id,
            )
            sealed = self._encode(asdict(new))
            db.execute(
                "UPDATE jobs SET state = ?, version = ?, updated = ?, sealed = ? "
                "WHERE id = ? AND version = ?",
                (
                    new.state,
                    new.version,
                    new.updated_at,
                    sealed,
                    row_id,
                    version,
                ),
            )
            changed: queue_merkle.JobRow = (
                row_id,
                row[1],
                row[2],
                new.state,
                new.version,
                new.updated_at,
                sealed,
            )
            self._commit_anchored(
                db,
                head,
                (self._row_change(row), None),
                (self._row_change(changed), None),
            )
            return new

    async def compare_and_set(
        self,
        call_id: str,
        version: int,
        state: QueueState,
        owner_id: str,
        *,
        provider_scope: str | None = None,
        attempt_id: str | None = None,
    ) -> CallJob | None:
        """Change state only for the current, nonterminal owner version."""
        return await asyncio.to_thread(
            self._compare_and_set, call_id, version, state, owner_id, provider_scope, attempt_id
        )

    def _recovery_transition_batch(
        self, jobs: tuple[CallJob, ...], owned_epoch: str, proof: str
    ) -> int:
        if not jobs or len(jobs) > 100 or not owned_epoch or ":" in owned_epoch:
            raise ValueError("invalid queue recovery batch")
        tenant_id = jobs[0].tenant_id
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            head = self._verify_anchor(db, verify_rows=False)
            before: dict[str, tuple[str, str, str, int, float, str]] = {}
            after: dict[str, tuple[str, str, str, int, float, str]] = {}
            for observed in jobs:
                if observed.tenant_id != tenant_id or not (
                    observed.owner_id == owned_epoch
                    or observed.owner_id.startswith(f"{owned_epoch}:")
                ):
                    raise QueueStateUnavailableError("queue recovery owner or tenant refused")
                row_id = _key(observed.call_id)
                row = db.execute(
                    "SELECT id, tenant_key, owner_key, state, version, updated, sealed "
                    "FROM jobs WHERE id = ?",
                    (row_id,),
                ).fetchone()
                self._verify_row(db, row_id, row, head.digest)
                if row is None:
                    raise QueueStateUnavailableError("unfinished recovery job disappeared")
                current = self._bound_job(row, observed.call_id)
                if current.version != observed.version or current.state in _TERMINAL:
                    continue
                if current != observed:
                    raise QueueStateUnavailableError("queue recovery snapshot changed")
                state: QueueState = "failed" if current.state == "queued" else "outcome_unknown"
                changed = replace(
                    current, state=state, version=current.version + 1, updated_at=time.time()
                )
                sealed = self._encode(asdict(changed))
                db.execute(
                    "UPDATE jobs SET state = ?, version = ?, updated = ?, sealed = ? "
                    "WHERE id = ? AND version = ?",
                    (state, changed.version, changed.updated_at, sealed, row_id, current.version),
                )
                before.update(self._row_change(row))
                after[row_id] = (
                    row[1],
                    row[2],
                    state,
                    changed.version,
                    changed.updated_at,
                    sealed,
                )
            if not after:
                return 0
            self._commit_anchored(
                db, head, (before, None), (after, None), (proof, tenant_id, owned_epoch)
            )
            return len(after)

    async def recovery_transition_batch(
        self,
        jobs: tuple[CallJob, ...],
        *,
        owned_epoch: str | None,
        proof: str | None,
    ) -> int:
        """Commit a bounded tenant batch through one atomic external fence CAS."""
        if owned_epoch is None or not proof or self._recovery_authority is None:
            raise QueueStateUnavailableError("durable queue recovery authority unavailable")
        return await asyncio.to_thread(self._recovery_transition_batch, jobs, owned_epoch, proof)

    def _validate_recovery_proofs(
        self, owned_epoch: str | None, proofs: Mapping[str, str]
    ) -> None:
        authority = self._recovery_authority
        if authority is None or not owned_epoch:
            raise QueueStateUnavailableError("durable queue recovery authority unavailable")
        try:
            for tenant_id, proof in proofs.items():
                if not proof:
                    raise ValueError("missing queue recovery proof")
                authority.validate(
                    proof,
                    journal_scope=self._anchor.scope,
                    tenant_id=tenant_id,
                    owner_epoch=owned_epoch,
                    purpose="queue.recover",
                )
        except Exception as exc:
            raise QueueStateUnavailableError("durable queue recovery proof refused") from exc

    async def validate_recovery_proofs(
        self, *, owned_epoch: str | None, proofs: Mapping[str, str]
    ) -> None:
        """Preflight all selected tenants before the first fenced batch CAS."""
        await asyncio.to_thread(self._validate_recovery_proofs, owned_epoch, proofs)

    def _list_jobs(self, tenant_id: str | None, offset: int, limit: int) -> list[CallJob]:
        scope = (
            queue_merkle.GLOBAL_SCOPE
            if tenant_id is None
            else queue_merkle.scope_key(queue_merkle.tenant_key(tenant_id), None, None)
        )
        try:
            with self._connect() as db:
                db.execute("BEGIN")
                head = self._verify_anchor(db, verify_rows=False)
                keys = queue_merkle.page_keys(
                    db, scope, head.digest, after=None, limit=offset + limit
                )
                return [
                    self._bound_job(queue_merkle.row_for_key(db, key)) for key in keys[offset:]
                ]
        except (ValueError, TypeError, sqlite3.Error) as exc:
            raise QueueStateUnavailableError("queue listing proof unavailable") from exc

    async def list_jobs(
        self, *, tenant_id: str | None = None, offset: int = 0, limit: int = 100
    ) -> list[CallJob]:
        """Return a bounded page; caller must authorize the requested scope."""
        if type(offset) is not int or offset < 0 or offset > self._history_limit:
            raise ValueError("invalid queue page")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid queue page")
        return await self._read_retry(self._list_jobs, tenant_id, offset, limit)

    async def metadata_page(
        self, scope: QueueReadScope, *, cursor: str | None, limit: int
    ) -> QueueMetadataPage:
        """Return one authenticated, tenant-indexed keyset page."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid queue page")
        return await self._read_retry(self._metadata_page, scope, cursor, limit)

    async def recovery_page(self, *, cursor: str | None, limit: int) -> QueueRecoveryPage:
        """Page an authenticated fixed ID set with current, fenced row versions."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid queue recovery page")
        return await self._read_retry(self._recovery_page, cursor, limit)

    def _recovery_page(self, cursor: str | None, limit: int) -> QueueRecoveryPage:
        with self._connect() as db:
            db.execute("BEGIN")
            head = self._verify_anchor(db, verify_rows=False)
            if cursor is None:
                self._recovery_snapshots.clear()
                keys = tuple(queue_merkle.all_id_keys(db, head.digest))
                count: int = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
                if count != len(keys) or count > self._history_limit:
                    raise QueueStateUnavailableError("queue recovery ID directory incomplete")
                try:
                    snapshot = tuple(
                        (key, queue_merkle.row_for_id_key(db, key)[3]) for key in keys
                    )
                except (ValueError, TypeError, sqlite3.Error) as exc:
                    raise QueueStateUnavailableError("queue recovery snapshot invalid") from exc
                position = 0
                created = time.monotonic()
            else:
                saved = self._recovery_snapshots.get(cursor)
                if saved is None or time.monotonic() - saved[2] > 3600:
                    raise QueueStateUnavailableError("queue recovery cursor unavailable")
                snapshot, position, created = saved
            jobs: list[CallJob] = []
            following = min(position + limit, len(snapshot))
            for key, previous_state in snapshot[position:following]:
                row_id = key[queue_merkle.SCOPE_LENGTH : queue_merkle.SCOPE_LENGTH + 64]
                row = db.execute(
                    "SELECT id, tenant_key, owner_key, state, version, updated, sealed "
                    "FROM jobs WHERE id = ?",
                    (row_id,),
                ).fetchone()
                self._verify_row(db, row_id, row, head.digest)
                if row is None:
                    if previous_state not in _TERMINAL:
                        raise QueueStateUnavailableError("unfinished recovery job disappeared")
                    continue
                jobs.append(self._bound_job(row))
            next_cursor = None
            if following < len(snapshot):
                next_cursor = uuid.uuid4().hex
                self._recovery_snapshots[next_cursor] = (snapshot, following, created)
                if len(self._recovery_snapshots) > 2:
                    self._recovery_snapshots.pop(next(iter(self._recovery_snapshots)))
            return QueueRecoveryPage(tuple(jobs), next_cursor)

    def _metadata_page(
        self, scope: QueueReadScope, cursor: str | None, limit: int
    ) -> QueueMetadataPage:
        tenant_key = queue_merkle.tenant_key(scope.tenant_id)
        owner_key = queue_merkle.owner_key(scope.owner_id) if scope.owner_id else None
        scope_key = queue_merkle.scope_key(tenant_key, owner_key, scope.state)
        after: str | None = None
        prior_scope_digest: str | None = None
        if cursor is not None:
            try:
                if len(cursor) > 4096:
                    raise ValueError("invalid queue cursor")
                raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
                data = self._decode(raw.decode("ascii"))
                if data.get("format") != 4 or data.get("scope") != scope_key:
                    raise ValueError("invalid queue cursor")
                prior_scope_digest = str(data["scope_digest"])
                if len(prior_scope_digest) != 64:
                    raise ValueError("invalid queue cursor")
                bytes.fromhex(prior_scope_digest)
                after = str(data["key"])
                if len(after) != queue_merkle.KEY_LENGTH or not after.startswith(scope_key):
                    raise ValueError("invalid queue cursor")
                bytes.fromhex(after)
            except (ValueError, TypeError, KeyError, UnicodeError) as exc:
                raise ValueError("invalid queue cursor") from exc
        try:
            with self._connect() as db:
                db.execute("BEGIN")
                head = self._verify_anchor(db, verify_rows=False)
                current_scope_digest = queue_merkle.scope_digest(db, scope_key, head.digest)
                if prior_scope_digest is None or prior_scope_digest == current_scope_digest:
                    keys = queue_merkle.page_keys(
                        db, scope_key, head.digest, after=after, limit=limit + 1
                    )
                else:
                    keys = []
                jobs = tuple(
                    self._bound_job(queue_merkle.row_for_key(db, key)) for key in keys[:limit]
                )
        except (ValueError, TypeError, sqlite3.Error) as exc:
            raise QueueStateUnavailableError("queue metadata proof unavailable") from exc
        if prior_scope_digest is not None and prior_scope_digest != current_scope_digest:
            raise ValueError("stale queue cursor")
        next_cursor = None
        if len(keys) > limit:
            payload = {
                "format": 4,
                "scope": scope_key,
                "scope_digest": current_scope_digest,
                "key": keys[limit - 1],
            }
            next_cursor = base64.urlsafe_b64encode(self._encode(payload).encode()).decode()
        return QueueMetadataPage(jobs, next_cursor)

    def _save_control(self, control: dict[str, Any], expected_revision: int) -> int | None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            head = self._verify_anchor(db, verify_rows=False)
            row = db.execute("SELECT sealed FROM controls WHERE id = 1").fetchone()
            previous = row[0] if row else None
            self._verify_control(db, previous, head.digest)
            current = self._decode(row[0])["revision"] if row else 0
            if current != expected_revision:
                return None
            revision = current + 1
            sealed = self._encode({**control, "revision": revision})
            db.execute(
                "INSERT INTO controls(id, sealed) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET sealed = excluded.sealed",
                (sealed,),
            )
            self._commit_anchored(db, head, ({}, previous), ({}, sealed))
            return revision

    async def save_control(self, control: dict[str, Any], expected_revision: int) -> int | None:
        """Persist pause and limits in the same encrypted journal."""
        return await asyncio.to_thread(self._save_control, control, expected_revision)

    def _load_control(self) -> dict[str, Any] | None:
        with self._connect() as db:
            db.execute("BEGIN")
            head = self._verify_anchor(db, verify_rows=False)
            row = db.execute("SELECT sealed FROM controls WHERE id = 1").fetchone()
            self._verify_control(db, row[0] if row else None, head.digest)
        return self._decode(row[0]) if row else None

    async def load_control(self) -> dict[str, Any] | None:
        """Read persisted pause and limits."""
        return await self._read_retry(self._load_control)
