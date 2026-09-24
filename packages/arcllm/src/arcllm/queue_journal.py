"""Optional encrypted SQLite queue journal behind CallQueueStore."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import stat
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, TypeVar

from arctrust import AnchorHead, MonotonicAnchor, RecordCipher

from arcllm.exceptions import QueueFullError, QueueStateUnavailableError
from arcllm.queue_control import (
    _TERMINAL,
    CallJob,
    QueueMetadataPage,
    QueueReadScope,
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
        cipher: RecordCipher,
        anchor: MonotonicAnchor,
        *,
        history_limit: int = 1000,
    ) -> None:
        if history_limit < 1:
            raise ValueError("history_limit must be positive")
        self._path = path
        self._cipher = cipher
        self._anchor = anchor
        self._history_limit = history_limit
        self._broken = False
        self._lock = threading.RLock()
        self._initialize()

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
        if self._path.exists():
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
            db.execute("PRAGMA journal_mode = WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, "
                "version INTEGER NOT NULL, updated REAL NOT NULL, sealed TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS controls "
                "(id INTEGER PRIMARY KEY CHECK(id = 1), sealed TEXT NOT NULL)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS queue_jobs_page_idx ON jobs(updated DESC, id DESC)"
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
        except QueueStateUnavailableError:
            self._broken = True
            raise
        except Exception as exc:
            self._broken = True
            raise QueueStateUnavailableError("queue anchor unavailable") from exc
        self._broken = False

    @staticmethod
    def _is_empty(db: sqlite3.Connection) -> bool:
        jobs: int = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        controls: int = db.execute("SELECT COUNT(*) FROM controls").fetchone()[0]
        return jobs == 0 and controls == 0

    @staticmethod
    def _digest(db: sqlite3.Connection) -> str:
        jobs = db.execute("SELECT id, version, updated, sealed FROM jobs ORDER BY id").fetchall()
        controls = db.execute("SELECT id, sealed FROM controls ORDER BY id").fetchall()
        payload = json.dumps([jobs, controls], separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _snapshot(db: sqlite3.Connection) -> tuple[dict[str, tuple[int, float, str]], str | None]:
        rows = db.execute("SELECT id, version, updated, sealed FROM jobs").fetchall()
        control = db.execute("SELECT sealed FROM controls WHERE id = 1").fetchone()
        jobs = {row[0]: (row[1], row[2], row[3]) for row in rows}
        return jobs, control[0] if control else None

    def _recover_one(self, db: sqlite3.Connection, head: AnchorHead, local: str) -> None:
        if head.previous_digest != local or not head.intent:
            raise QueueStateUnavailableError("queue journal rollback exceeds one verified intent")
        try:
            intent = self._decode(head.intent)
            if set(intent) != {"put", "delete", "control"}:
                raise ValueError("invalid queue intent")
            db.execute("BEGIN IMMEDIATE")
            if self._digest(db) != local:
                raise ValueError("queue journal changed during recovery")
            for key in intent["delete"]:
                db.execute("DELETE FROM jobs WHERE id = ?", (key,))
            for key, version, updated, sealed in intent["put"]:
                self._bound_job((key, version, updated, sealed))
                db.execute(
                    "INSERT INTO jobs(id, version, updated, sealed) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET version=excluded.version, "
                    "updated=excluded.updated, sealed=excluded.sealed",
                    (key, version, updated, sealed),
                )
            if intent["control"] is not None:
                self._decode(intent["control"])
                db.execute(
                    "INSERT INTO controls(id, sealed) VALUES (1, ?) "
                    "ON CONFLICT(id) DO UPDATE SET sealed=excluded.sealed",
                    (intent["control"],),
                )
            if self._digest(db) != head.digest:
                raise ValueError("replayed queue intent does not match anchor")
            db.commit()
        except Exception as exc:
            db.rollback()
            raise QueueStateUnavailableError("queue recovery intent invalid") from exc

    def _intent(
        self,
        before: tuple[dict[str, tuple[int, float, str]], str | None],
        after: tuple[dict[str, tuple[int, float, str]], str | None],
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

    def _verify_anchor(self, db: sqlite3.Connection) -> AnchorHead:
        try:
            head = self._anchor.latest()
            if head is None or head.scope != self._anchor.scope or head.digest != self._digest(db):
                raise QueueStateUnavailableError("queue journal diverged from anchor")
            return head
        except QueueStateUnavailableError:
            self._broken = True
            raise
        except Exception as exc:
            self._broken = True
            raise QueueStateUnavailableError("queue anchor unavailable") from exc

    def _commit_anchored(
        self,
        db: sqlite3.Connection,
        previous: AnchorHead,
        before: tuple[dict[str, tuple[int, float, str]], str | None],
    ) -> None:
        self._broken = True
        try:
            digest = self._digest(db)
            intent = self._intent(before, self._snapshot(db))
            head = self._anchor.compare_and_advance(previous, digest, intent)
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

    def _encode(self, value: dict[str, Any]) -> str:
        return json.dumps(self._cipher.seal({"extra": value})["extra"], separators=(",", ":"))

    def _decode(self, value: str) -> dict[str, Any]:
        sealed = json.loads(value)
        if not isinstance(sealed, dict) or set(sealed) != {"arc.audit.sealed"}:
            raise ValueError("queue journal record is not sealed")
        decoded: dict[str, Any] = self._cipher.unseal({"extra": sealed})["extra"]
        return decoded

    def _bound_job(self, row: tuple[str, int, float, str], call_id: str | None = None) -> CallJob:
        key, version, updated, sealed = row
        job = CallJob(**self._decode(sealed))
        if _key(job.call_id) != key or job.version != version or job.updated_at != updated:
            raise ValueError("queue journal row binding failed")
        if call_id is not None and job.call_id != call_id:
            raise ValueError("queue journal call binding failed")
        return job

    def _create(self, job: CallJob) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            head = self._verify_anchor(db)
            before = self._snapshot(db)
            rows = db.execute(
                "SELECT id, version, updated, sealed FROM jobs ORDER BY updated ASC"
            ).fetchall()
            excess = len(rows) - self._history_limit + 1
            for row in rows:
                if excess <= 0:
                    break
                existing = self._bound_job(row)
                if existing.state in _TERMINAL:
                    db.execute("DELETE FROM jobs WHERE id = ?", (row[0],))
                    excess -= 1
            if excess > 0:
                raise QueueFullError(len(rows), self._history_limit)
            db.execute(
                "INSERT INTO jobs(id, version, updated, sealed) VALUES (?, ?, ?, ?)",
                (_key(job.call_id), job.version, job.updated_at, self._encode(asdict(job))),
            )
            self._commit_anchored(db, head, before)

    async def create(self, job: CallJob) -> None:
        """Persist an accepted call before any provider admission."""
        await asyncio.to_thread(self._create, job)

    def _get(self, call_id: str) -> CallJob | None:
        with self._connect() as db:
            db.execute("BEGIN")
            self._verify_anchor(db)
            row = db.execute(
                "SELECT id, version, updated, sealed FROM jobs WHERE id = ?", (_key(call_id),)
            ).fetchone()
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
            head = self._verify_anchor(db)
            before = self._snapshot(db)
            row = db.execute(
                "SELECT id, version, updated, sealed FROM jobs WHERE id = ? AND version = ?",
                (_key(call_id), version),
            ).fetchone()
            if row is None:
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
            db.execute(
                "UPDATE jobs SET version = ?, updated = ?, sealed = ? "
                "WHERE id = ? AND version = ?",
                (new.version, new.updated_at, self._encode(asdict(new)), _key(call_id), version),
            )
            self._commit_anchored(db, head, before)
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

    def _list_jobs(self, tenant_id: str | None, offset: int, limit: int) -> list[CallJob]:
        with self._connect() as db:
            db.execute("BEGIN")
            self._verify_anchor(db)
            rows = db.execute(
                "SELECT id, version, updated, sealed FROM jobs ORDER BY updated DESC"
            ).fetchall()
        jobs = [self._bound_job(row) for row in rows]
        if tenant_id is not None:
            jobs = [job for job in jobs if job.tenant_id == tenant_id]
        return jobs[offset : offset + limit]

    async def list_jobs(
        self, *, tenant_id: str | None = None, offset: int = 0, limit: int = 100
    ) -> list[CallJob]:
        """Return a bounded page; caller must authorize the requested scope."""
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("invalid queue page")
        return await self._read_retry(self._list_jobs, tenant_id, offset, limit)

    async def metadata_page(
        self, scope: QueueReadScope, *, cursor: str | None, limit: int
    ) -> QueueMetadataPage:
        """Refuse durable scoped paging until an authenticated tenant index exists."""
        if not 1 <= limit <= 100:
            raise ValueError("invalid queue page")
        raise QueueStateUnavailableError("durable scoped queue paging is unavailable")

    def _save_control(self, control: dict[str, Any], expected_revision: int) -> int | None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            head = self._verify_anchor(db)
            before = self._snapshot(db)
            row = db.execute("SELECT sealed FROM controls WHERE id = 1").fetchone()
            current = self._decode(row[0])["revision"] if row else 0
            if current != expected_revision:
                return None
            revision = current + 1
            db.execute(
                "INSERT INTO controls(id, sealed) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET sealed = excluded.sealed",
                (self._encode({**control, "revision": revision}),),
            )
            self._commit_anchored(db, head, before)
            return revision

    async def save_control(self, control: dict[str, Any], expected_revision: int) -> int | None:
        """Persist pause and limits in the same encrypted journal."""
        return await asyncio.to_thread(self._save_control, control, expected_revision)

    def _load_control(self) -> dict[str, Any] | None:
        with self._connect() as db:
            db.execute("BEGIN")
            self._verify_anchor(db)
            row = db.execute("SELECT sealed FROM controls WHERE id = 1").fetchone()
        return self._decode(row[0]) if row else None

    async def load_control(self) -> dict[str, Any] | None:
        """Read persisted pause and limits."""
        return await self._read_retry(self._load_control)
