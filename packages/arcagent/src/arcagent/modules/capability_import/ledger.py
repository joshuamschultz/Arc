"""Agent-local, atomic lifecycle ledger for capability-import reviews."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from arctrust import AuditEvent, AuditSink, emit

from arcagent.modules.capability_import.models import CapabilityImportStatus


class ImportLedger:
    """Persist only review state; signing and activation are separate concerns."""

    def __init__(self, capabilities_root: Path, *, audit_sink: AuditSink | None = None) -> None:
        self._path = Path(capabilities_root) / "imports" / "ledger.json"
        self._lock_path = self._path.with_suffix(".lock")
        self._audit_sink = audit_sink

    def get(self, import_id: str) -> dict[str, Any] | None:
        with self._locked():
            return self._read().get(import_id)

    def set(self, import_id: str, status: CapabilityImportStatus, **data: Any) -> dict[str, Any]:
        """Atomically record one allowed non-activating lifecycle state."""
        with self._locked():
            ledger = self._read()
            row = {"status": status.value, **data}
            ledger[import_id] = row
            self._write(ledger)
        self._emit(import_id, status)
        return row

    def restore(self, import_id: str, row: dict[str, Any]) -> None:
        """Restore a previously committed row during a failed composition."""
        with self._locked():
            ledger = self._read()
            ledger[import_id] = dict(row)
            self._write(ledger)

    def _read(self) -> dict[str, dict[str, Any]]:
        if not self._path.is_file():
            return {}
        try:
            parsed = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("capability import ledger is unreadable") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError("capability import ledger is malformed")
        return parsed

    def _write(self, data: dict[str, dict[str, Any]]) -> None:
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self._path.with_name(".ledger.tmp")
        payload = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
        temporary.write_bytes(payload)
        temporary.chmod(0o600)
        os.replace(temporary, self._path)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        import fcntl

        self._lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._lock_path.open("a+b") as lock:
            self._lock_path.chmod(0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _emit(self, import_id: str, status: CapabilityImportStatus) -> None:
        if self._audit_sink is None:
            return
        emit(
            AuditEvent(
                actor_did="did:arc:capability-import",
                action=f"capability_import.{status.value}",
                target=import_id,
                outcome=status.value,
            ),
            self._audit_sink,
        )
