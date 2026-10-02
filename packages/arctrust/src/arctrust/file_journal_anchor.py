"""Operator-signed local file journal implementing :class:`MonotonicAnchor`.

The zero-config revision authority for personal and enterprise tier. Each scope
owns two files under one directory (resolved by the caller through
:func:`arctrust.paths.skill_revision_anchor_dir`):

* ``<sha256(scope)>.jsonl`` — an append-only journal. Every line is one head,
  signed by the operator signing capability and hash-chained to the line before
  it (``prev_entry``), with ``version`` strictly ``n`` and ``previous_digest``
  equal to the prior line's digest.
* ``<sha256(scope)>.head`` — an operator-signed seal naming the newest version
  and its line hash. The seal is written before the first line (version 0) and
  after every append, so a journal that is shorter than its seal is a rollback,
  and a journal without a seal is tampering.

What this defends: edits to any line, forged lines or whole journals signed by
another key, entries replayed inside a journal or copied from another scope,
truncation behind the seal, symlink substitution, and any regression or fork
seen by a running process. What it cannot defend: an attacker holding the
filesystem who restores a consistent *older copy of both files* to a process
that has not yet read the newer head. That is why it is a LOCAL anchor: every
advance is audited with ``custody="local_file"``, and federal deployments floor
to an externally custodied anchor (Vault KV or the queue broker) instead.

The anchor never sees key material — it is handed a :class:`Signer` capability
and verifies with that signer's public key.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from arctrust.audit import AuditEvent, AuditSink, emit
from arctrust.monotonic import AnchorHead, AnchorUnavailableError
from arctrust.signer import Signer, verify_signature

LOCAL_ANCHOR_CUSTODY = "local_file"
"""Audit ``custody`` value carried by every local-anchor event (audit-warn)."""

_DOMAIN = b"arc.file-journal-anchor.v1\n"
_GENESIS = "0" * 64
_SCOPE_RE = re.compile(r"^[a-zA-Z0-9_/-]{1,256}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_INTENT = 1_048_576
_MAX_FILE = 64 * 1024 * 1024
_MAX_ENTRIES = 10_000
_ENTRY_KEYS = frozenset(
    {
        "kind",
        "scope",
        "version",
        "digest",
        "previous_digest",
        "intent",
        "prev_entry",
        "algorithm",
        "public_key",
        "signature",
    }
)
_SEAL_KEYS = frozenset(
    {"kind", "scope", "version", "entry_hash", "algorithm", "public_key", "signature"}
)


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _Journal:
    """Verified on-disk state: entries, their line hashes, and the byte length kept."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []
        self.hashes: list[str] = []
        self.valid_length = 0
        self.sealed = False

    def head(self) -> AnchorHead | None:
        if not self.entries:
            return None
        last = self.entries[-1]
        return AnchorHead(
            scope=last["scope"],
            version=last["version"],
            digest=last["digest"],
            previous_digest=last["previous_digest"],
            intent=last["intent"],
        )


class FileJournalAnchor:
    """Operator-signed, hash-chained local CAS head for one scope."""

    def __init__(
        self,
        directory: Path,
        *,
        scope: str,
        signer: Signer,
        audit_sink: AuditSink | None = None,
        actor_did: str = "",
    ) -> None:
        if type(scope) is not str or not _SCOPE_RE.fullmatch(scope) or ".." in scope:
            raise ValueError("invalid anchor scope")
        self._directory = Path(directory)
        self._scope = scope
        self._signer = signer
        self._public_key = signer.public_key.hex()
        self._algorithm = signer.algorithm
        self._audit_sink = audit_sink
        self._actor_did = actor_did or "arctrust:file-journal-anchor"
        stem = _sha256(scope.encode("utf-8"))
        self._journal = self._directory / f"{stem}.jsonl"
        self._seal = self._directory / f"{stem}.head"
        self._lockfile = self._directory / f"{stem}.lock"
        self._seen: AnchorHead | None = None
        self._mutex = threading.RLock()

    @property
    def scope(self) -> str:
        """Stable namespace bound into every signed line."""
        return self._scope

    # ------------------------------------------------------------------ reads

    def latest(self) -> AnchorHead | None:
        """Verify the whole journal and seal; return the newest signed head."""
        with self._mutex, self._locked(create=False):
            head = self._read_state().head()
            self._check_monotonic(head)
            return head

    # ----------------------------------------------------------------- writes

    def compare_and_advance(
        self, expected: AnchorHead | None, digest: str, intent: str
    ) -> AnchorHead:
        """Append one signed head iff ``expected`` is still the current head."""
        if type(digest) is not str or not _DIGEST_RE.fullmatch(digest):
            raise ValueError("invalid anchor digest")
        if type(intent) is not str or len(intent) > _MAX_INTENT:
            raise ValueError("invalid anchor intent")
        with self._mutex, self._locked(create=True):
            try:
                state = self._read_state()
                current = state.head()
                self._check_monotonic(current)
                if current != expected:
                    raise AnchorUnavailableError("local anchor CAS owner is stale")
                head = self._append(state, current, digest, intent)
            except AnchorUnavailableError:
                self._audit("deny", None)
                raise
        self._seen = head
        self._audit("allow", head)
        return head

    def _append(
        self, state: _Journal, current: AnchorHead | None, digest: str, intent: str
    ) -> AnchorHead:
        if not state.sealed:
            self._write_seal(0, _GENESIS)
        version = current.version + 1 if current else 1
        entry: dict[str, Any] = {
            "kind": "entry",
            "scope": self._scope,
            "version": version,
            "digest": digest,
            "previous_digest": current.digest if current else None,
            "intent": intent,
            "prev_entry": state.hashes[-1] if state.hashes else _GENESIS,
            "algorithm": self._algorithm,
            "public_key": self._public_key,
        }
        entry["signature"] = self._sign(entry)
        line = _canonical(entry)
        descriptor = os.open(self._journal, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            # Drop a torn, never-sealed tail before appending after it.
            os.ftruncate(descriptor, state.valid_length)
            os.lseek(descriptor, state.valid_length, os.SEEK_SET)
            os.write(descriptor, line + b"\n")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._write_seal(version, _sha256(line))
        return AnchorHead(
            scope=self._scope,
            version=version,
            digest=digest,
            previous_digest=entry["previous_digest"],
            intent=intent,
        )

    # --------------------------------------------------------------- internals

    @contextmanager
    def _locked(self, *, create: bool) -> Iterator[None]:
        """Serialize every reader and writer of this scope across processes."""
        if self._directory.is_symlink():
            raise AnchorUnavailableError("local anchor directory is a symlink")
        if not self._directory.exists():
            if not create:
                yield
                return
            self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            descriptor = os.open(self._lockfile, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError as exc:
            raise AnchorUnavailableError("local anchor lock is unavailable") from exc
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _read_file(self, path: Path) -> bytes | None:
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise AnchorUnavailableError("local anchor file is unsafe") from exc
        try:
            status = os.fstat(descriptor)
            if not stat.S_ISREG(status.st_mode) or status.st_size > _MAX_FILE:
                raise AnchorUnavailableError("local anchor file is unsafe")
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 1024 * 1024):
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    def _read_state(self) -> _Journal:
        journal_bytes = self._read_file(self._journal)
        seal_bytes = self._read_file(self._seal)
        state = _Journal()
        if seal_bytes is None:
            if journal_bytes:
                raise AnchorUnavailableError("local anchor seal is missing")
            return state
        state.sealed = True
        sealed_version, sealed_hash = self._verify_seal(seal_bytes)
        self._read_entries(journal_bytes or b"", state)
        if sealed_version > len(state.entries):
            raise AnchorUnavailableError("local anchor journal rolled back behind its seal")
        expected_hash = state.hashes[sealed_version - 1] if sealed_version else _GENESIS
        if expected_hash != sealed_hash:
            raise AnchorUnavailableError("local anchor journal does not match its seal")
        return state

    def _read_entries(self, data: bytes, state: _Journal) -> None:
        # A trailing fragment without a newline is a torn, never-sealed append.
        complete, _, _torn = data.rpartition(b"\n")
        lines = complete.split(b"\n") if complete else []
        if len(lines) > _MAX_ENTRIES:
            raise AnchorUnavailableError("local anchor journal exceeds resource limits")
        for line in lines:
            self._verify_entry(line, state)
        state.valid_length = len(complete) + 1 if complete else 0

    def _verify_entry(self, line: bytes, state: _Journal) -> None:
        record = self._verified_record(line, _ENTRY_KEYS, "entry")
        index = len(state.entries)
        previous = state.entries[-1] if state.entries else None
        if (
            type(record["version"]) is not int
            or record["version"] != index + 1
            or record["prev_entry"] != (state.hashes[-1] if state.hashes else _GENESIS)
            or record["previous_digest"] != (previous["digest"] if previous else None)
            or type(record["digest"]) is not str
            or not _DIGEST_RE.fullmatch(record["digest"])
            or type(record["intent"]) is not str
        ):
            raise AnchorUnavailableError("local anchor journal chain is broken")
        state.entries.append(record)
        state.hashes.append(_sha256(line))

    def _verify_seal(self, data: bytes) -> tuple[int, str]:
        record = self._verified_record(data, _SEAL_KEYS, "seal")
        version, entry_hash = record["version"], record["entry_hash"]
        if (
            type(version) is not int
            or version < 0
            or type(entry_hash) is not str
            or not _DIGEST_RE.fullmatch(entry_hash)
        ):
            raise AnchorUnavailableError("local anchor seal is invalid")
        return version, entry_hash

    def _verified_record(self, data: bytes, keys: frozenset[str], kind: str) -> dict[str, Any]:
        try:
            record = json.loads(data)
            if not isinstance(record, dict) or set(record) != keys:
                raise ValueError("shape")
            unsigned = {key: value for key, value in record.items() if key != "signature"}
            signature = bytes.fromhex(record["signature"])
        except (ValueError, TypeError) as exc:
            raise AnchorUnavailableError("local anchor record is malformed") from exc
        if (
            record["kind"] != kind
            or record["scope"] != self._scope
            or record["public_key"] != self._public_key
            or record["algorithm"] != self._algorithm
            or not verify_signature(
                self._algorithm,
                _DOMAIN + _canonical(unsigned),
                signature,
                bytes.fromhex(self._public_key),
            )
        ):
            raise AnchorUnavailableError("local anchor record signature is invalid")
        return record

    def _sign(self, unsigned: dict[str, Any]) -> str:
        try:
            return self._signer.sign(_DOMAIN + _canonical(unsigned)).hex()
        except Exception as exc:  # reason: any signer failure must fail closed
            raise AnchorUnavailableError("local anchor signing is unavailable") from exc

    def _write_seal(self, version: int, entry_hash: str) -> None:
        seal: dict[str, Any] = {
            "kind": "seal",
            "scope": self._scope,
            "version": version,
            "entry_hash": entry_hash,
            "algorithm": self._algorithm,
            "public_key": self._public_key,
        }
        seal["signature"] = self._sign(seal)
        temporary = self._seal.with_name(self._seal.name + ".tmp")
        temporary.unlink(missing_ok=True)
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        try:
            os.write(descriptor, _canonical(seal))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, self._seal)
        directory = os.open(self._directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _check_monotonic(self, head: AnchorHead | None) -> None:
        seen = self._seen
        if seen is not None and (
            head is None
            or head.version < seen.version
            or (head.version == seen.version and head != seen)
        ):
            raise AnchorUnavailableError("local anchor regressed or forked")
        self._seen = head

    def _audit(self, outcome: str, head: AnchorHead | None) -> None:
        if self._audit_sink is None:
            return
        extra: dict[str, Any] = {"custody": LOCAL_ANCHOR_CUSTODY}
        if head is not None:
            extra["version"] = head.version
            extra["digest"] = head.digest
        emit(
            AuditEvent(
                actor_did=self._actor_did,
                action="revision_anchor.advance",
                target=self._scope,
                outcome=outcome,
                extra=extra,
            ),
            self._audit_sink,
        )


__all__ = ["LOCAL_ANCHOR_CUSTODY", "FileJournalAnchor"]
