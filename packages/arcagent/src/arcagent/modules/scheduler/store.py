"""JSON file persistence for schedule entries — SPEC-002.

Atomic writes via tempfile + fsync + os.replace to prevent partial writes.
Single-process sequential access — no file locking needed for MVP.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

from arcagent.modules.scheduler.models import ScheduleEntry

# File permissions: owner read/write only (0o600).
_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR


class ScheduleStore:
    """CRUD persistence for schedule entries backed by a JSON file."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> list[ScheduleEntry]:
        """Load all schedule entries from disk.

        Returns empty list if file is missing.
        """
        if not self._path.exists():
            return []
        raw = self._path.read_text(encoding="utf-8")
        data = json.loads(raw)
        return [ScheduleEntry(**item) for item in data]

    def save(self, entries: list[ScheduleEntry]) -> None:
        """Atomically write entries to disk.

        Uses mkstemp in the same directory + fsync + os.replace
        to guarantee no partial writes.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)

        json_bytes = json.dumps(
            [e.model_dump() for e in entries],
            indent=2,
        ).encode("utf-8")

        fd, tmp_path = tempfile.mkstemp(
            dir=self._path.parent,
            prefix=f".{self._path.name}.",
            suffix=".tmp",
        )
        fd_closed = False
        try:
            os.write(fd, json_bytes)
            os.fsync(fd)
            os.close(fd)
            fd_closed = True
            os.chmod(tmp_path, _FILE_MODE)
            os.replace(tmp_path, str(self._path))
        except Exception:
            if not fd_closed:
                os.close(fd)
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    def add(self, entry: ScheduleEntry) -> None:
        """Append a schedule entry and persist."""
        entries = self.load()
        entries.append(entry)
        self.save(entries)

    def update(self, schedule_id: str, updates: dict[str, object]) -> ScheduleEntry:
        """Update fields on an existing entry. Raises KeyError if not found."""
        entries = self.load()
        for i, entry in enumerate(entries):
            if entry.id == schedule_id:
                data = entry.model_dump()
                data.update(updates)
                entries[i] = ScheduleEntry(**data)
                self.save(entries)
                return entries[i]
        msg = f"Schedule '{schedule_id}' not found"
        raise KeyError(msg)

    def remove(self, schedule_id: str) -> None:
        """Remove a schedule entry by ID. No-op if not found."""
        entries = self.load()
        filtered = [e for e in entries if e.id != schedule_id]
        self.save(filtered)

    def get(self, schedule_id: str) -> ScheduleEntry | None:
        """Get a schedule entry by ID, or None if not found."""
        entries = self.load()
        for entry in entries:
            if entry.id == schedule_id:
                return entry
        return None
