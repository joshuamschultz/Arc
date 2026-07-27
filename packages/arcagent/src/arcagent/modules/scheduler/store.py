"""JSON file persistence for schedule entries — SPEC-002.

Atomic writes via tempfile + fsync + os.replace to prevent partial writes.
Single-process sequential access — no file locking needed for MVP.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from arcagent.modules.scheduler.models import ScheduleEntry

_logger = logging.getLogger("arcagent.modules.scheduler.store")

# File permissions: owner read/write only (0o600).
_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR


class ScheduleStore:
    """CRUD persistence for schedule entries backed by a JSON file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        # Rows from the last load() that failed validation. Held so save() can
        # write them back untouched — see load() for why they aren't dropped.
        self._invalid: list[dict[str, Any]] = []
        self._reported: set[str] = set()

    def load(self) -> list[ScheduleEntry]:
        """Load all schedule entries from disk, skipping any that don't validate.

        Returns empty list if file is missing.

        A row that fails validation is skipped rather than raised: the engine's
        timer loop calls this every tick, so one bad row used to raise until the
        breaker tripped and stopped ALL scheduling for the agent. Bad rows are
        kept (see :meth:`save`) because they are still the operator's data, and
        reported once each rather than on every tick.
        """
        if not self._path.exists():
            return []
        raw = self._path.read_text(encoding="utf-8")
        data = json.loads(raw)

        entries: list[ScheduleEntry] = []
        self._invalid = []
        for item in data:
            try:
                entries.append(ScheduleEntry(**item))
            except ValidationError as exc:
                self._invalid.append(item)
                self._report_invalid(item, exc)
        return entries

    def _report_invalid(self, item: dict[str, Any], error: ValidationError) -> None:
        """Log an unloadable row once per id, so a 30s poll doesn't spam."""
        schedule_id = str(item.get("id", "<no id>"))
        if schedule_id in self._reported:
            return
        self._reported.add(schedule_id)
        _logger.error(
            "Schedule %s in %s is invalid and will never run — skipping it; "
            "the agent's other schedules are unaffected: %s",
            schedule_id,
            self._path,
            error,
        )

    def save(self, entries: list[ScheduleEntry]) -> None:
        """Atomically write entries to disk, preserving unloadable rows.

        Uses mkstemp in the same directory + fsync + os.replace
        to guarantee no partial writes.

        Rows the last :meth:`load` could not validate are appended back. Without
        this the first metadata write after a fired schedule would silently
        delete a row the operator can still see in arcui.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)

        json_bytes = json.dumps(
            [e.model_dump() for e in entries] + self._invalid,
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
        except Exception:  # reason: re-raise after log
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

    def update(
        self,
        schedule_id: str,
        updates: dict[str, object],
        *,
        context: dict[str, int] | None = None,
    ) -> ScheduleEntry:
        """Update fields on an existing entry. Raises KeyError if not found.

        ``context`` is forwarded to model validation so config-derived limits
        (interval floor, timeout ceiling) are enforced on updated fields.
        """
        entries = self.load()
        for i, entry in enumerate(entries):
            if entry.id == schedule_id:
                data = entry.model_dump()
                data.update(updates)
                entries[i] = ScheduleEntry.model_validate(data, context=context)
                self.save(entries)
                return entries[i]
        msg = f"Schedule '{schedule_id}' not found"
        raise KeyError(msg)

    def remove(self, schedule_id: str) -> None:
        """Remove a schedule entry by ID. No-op if not found.

        Also drops an unloadable row with that id — preserving bad rows across
        saves must not make them undeletable, or the only way to clear one is
        hand-editing the file, which is how they get there.
        """
        entries = self.load()
        filtered = [e for e in entries if e.id != schedule_id]
        self._invalid = [row for row in self._invalid if row.get("id") != schedule_id]
        self._reported.discard(schedule_id)
        self.save(filtered)

    def get(self, schedule_id: str) -> ScheduleEntry | None:
        """Get a schedule entry by ID, or None if not found."""
        entries = self.load()
        for entry in entries:
            if entry.id == schedule_id:
                return entry
        return None
