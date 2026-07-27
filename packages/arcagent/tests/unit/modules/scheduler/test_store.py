"""Unit tests for scheduler store — SPEC-002 Phase 2."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from arcagent.modules.scheduler.models import (
    ActiveHours,
    ScheduleEntry,
)
from arcagent.modules.scheduler.store import ScheduleStore


def _make_entry(
    id: str = "sched_abc123",  # noqa: A002 - matches ScheduleEntry field
    type: str = "interval",  # noqa: A002 - matches ScheduleEntry field
    prompt: str = "Heartbeat check",
    every_seconds: int = 300,
    **kwargs: object,
) -> ScheduleEntry:
    """Helper to create a valid ScheduleEntry for tests."""
    return ScheduleEntry(
        id=id,
        type=type,
        prompt=prompt,
        every_seconds=every_seconds,
        **kwargs,
    )


class TestStoreLoad:
    def test_load_missing_file(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path / "nonexistent.json")
        entries = store.load()
        assert entries == []

    def test_load_empty_file(self, tmp_path: Path) -> None:
        path = tmp_path / "schedules.json"
        path.write_text("[]")
        store = ScheduleStore(path)
        entries = store.load()
        assert entries == []

    def test_load_with_entries(self, tmp_path: Path) -> None:
        entry = _make_entry()
        path = tmp_path / "schedules.json"
        path.write_text(json.dumps([entry.model_dump()]))
        store = ScheduleStore(path)
        entries = store.load()
        assert len(entries) == 1
        assert entries[0].id == "sched_abc123"
        assert entries[0].every_seconds == 300


class TestStoreSave:
    def test_save_creates_file(self, tmp_path: Path) -> None:
        path = tmp_path / "schedules.json"
        store = ScheduleStore(path)
        entry = _make_entry()
        store.save([entry])
        assert path.exists()

    def test_save_and_load_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "schedules.json"
        store = ScheduleStore(path)
        entry = _make_entry(
            active_hours=ActiveHours(start="08:00", end="18:00"),
        )
        store.save([entry])
        loaded = store.load()
        assert len(loaded) == 1
        assert loaded[0].id == entry.id
        assert loaded[0].active_hours is not None
        assert loaded[0].active_hours.start == "08:00"

    def test_save_overwrites_existing(self, tmp_path: Path) -> None:
        path = tmp_path / "schedules.json"
        store = ScheduleStore(path)
        store.save([_make_entry(id="sched_first")])
        store.save([_make_entry(id="sched_second")])
        loaded = store.load()
        assert len(loaded) == 1
        assert loaded[0].id == "sched_second"

    def test_save_file_permissions(self, tmp_path: Path) -> None:
        path = tmp_path / "schedules.json"
        store = ScheduleStore(path)
        store.save([_make_entry()])
        mode = stat.S_IMODE(os.stat(path).st_mode)
        # Owner read/write, no world access
        assert mode & stat.S_IROTH == 0
        assert mode & stat.S_IWOTH == 0


class TestStoreAdd:
    def test_add_to_empty(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path / "schedules.json")
        entry = _make_entry()
        store.add(entry)
        loaded = store.load()
        assert len(loaded) == 1
        assert loaded[0].id == entry.id

    def test_add_appends(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path / "schedules.json")
        store.add(_make_entry(id="sched_first"))
        store.add(_make_entry(id="sched_second"))
        loaded = store.load()
        assert len(loaded) == 2
        ids = {e.id for e in loaded}
        assert ids == {"sched_first", "sched_second"}


class TestStoreUpdate:
    def test_update_field(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path / "schedules.json")
        store.add(_make_entry(id="sched_upd"))
        updated = store.update("sched_upd", {"enabled": False})
        assert updated.enabled is False
        # Verify persisted
        loaded = store.load()
        assert loaded[0].enabled is False

    def test_update_prompt(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path / "schedules.json")
        store.add(_make_entry(id="sched_upd"))
        updated = store.update("sched_upd", {"prompt": "Updated prompt"})
        assert updated.prompt == "Updated prompt"

    def test_update_nonexistent_raises(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path / "schedules.json")
        with pytest.raises(KeyError):
            store.update("sched_missing", {"enabled": False})


class TestStoreRemove:
    def test_remove_existing(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path / "schedules.json")
        store.add(_make_entry(id="sched_rm"))
        store.remove("sched_rm")
        loaded = store.load()
        assert len(loaded) == 0

    def test_remove_nonexistent_is_noop(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path / "schedules.json")
        store.add(_make_entry(id="sched_keep"))
        store.remove("sched_missing")
        loaded = store.load()
        assert len(loaded) == 1


class TestStoreGet:
    def test_get_found(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path / "schedules.json")
        store.add(_make_entry(id="sched_find"))
        result = store.get("sched_find")
        assert result is not None
        assert result.id == "sched_find"

    def test_get_not_found(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path / "schedules.json")
        result = store.get("sched_missing")
        assert result is None


class TestStoreAtomicWrite:
    def test_no_partial_write_on_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Simulate crash during write — original file should be untouched."""
        path = tmp_path / "schedules.json"
        store = ScheduleStore(path)
        store.save([_make_entry(id="sched_original")])

        # Patch os.replace to simulate crash after write but before rename.
        # store.save catches Exception (not BaseException) to avoid
        # swallowing KeyboardInterrupt/SystemExit.
        original_replace = os.replace

        def crashing_replace(src: str, dst: str) -> None:
            # Remove temp file (simulating crash cleanup) but don't rename
            os.unlink(src)
            raise OSError("Simulated crash")

        monkeypatch.setattr("os.replace", crashing_replace)

        with pytest.raises(OSError, match="Simulated crash"):
            store.save([_make_entry(id="sched_new")])

        # Original file should be intact
        monkeypatch.setattr("os.replace", original_replace)
        loaded = store.load()
        assert len(loaded) == 1
        assert loaded[0].id == "sched_original"

    def test_no_temp_files_left_on_success(self, tmp_path: Path) -> None:
        path = tmp_path / "schedules.json"
        store = ScheduleStore(path)
        store.save([_make_entry()])
        # Only the target file should exist, no .tmp leftovers
        files = list(tmp_path.iterdir())
        assert len(files) == 1
        assert files[0].name == "schedules.json"


class TestStorePoisonRowTolerance:
    """One unloadable row must not take the whole store — and the engine — down.

    ``load()`` used to build every row eagerly, so a single bad field raised on
    every timer tick until the engine's breaker tripped and stopped scheduling
    for that agent entirely (silently, while arcui still showed the schedules
    as enabled). A bad row is now skipped, kept on disk, and still deletable.
    """

    def _write(self, path: Path, rows: list[dict[str, object]]) -> None:
        path.write_text(json.dumps(rows), encoding="utf-8")

    def _poison_row(self) -> dict[str, object]:
        """A row that fails validation: created_by is a closed Literal."""
        row = _make_entry(id="sched_bad").model_dump()
        row["metadata"]["created_by"] = "operator"  # type: ignore[index]
        return row

    def test_load_skips_the_bad_row_and_returns_the_good_ones(self, tmp_path: Path) -> None:
        path = tmp_path / "schedules.json"
        good = _make_entry(id="sched_good").model_dump()
        self._write(path, [good, self._poison_row()])

        entries = ScheduleStore(path).load()

        assert [e.id for e in entries] == ["sched_good"]

    def test_save_preserves_the_bad_row_instead_of_erasing_it(self, tmp_path: Path) -> None:
        """A row we cannot parse is still the operator's data — never dropped."""
        path = tmp_path / "schedules.json"
        good = _make_entry(id="sched_good").model_dump()
        self._write(path, [good, self._poison_row()])
        store = ScheduleStore(path)

        store.save(store.load())  # the metadata write every fired schedule does

        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert {row["id"] for row in on_disk} == {"sched_good", "sched_bad"}

    def test_bad_row_can_still_be_removed(self, tmp_path: Path) -> None:
        """Preserving it must not make it undeletable — else the only way out
        is hand-editing the file, which is what creates poison rows."""
        path = tmp_path / "schedules.json"
        good = _make_entry(id="sched_good").model_dump()
        self._write(path, [good, self._poison_row()])
        store = ScheduleStore(path)
        store.load()

        store.remove("sched_bad")

        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert [row["id"] for row in on_disk] == ["sched_good"]

    def test_bad_row_is_reported_once_not_every_tick(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The timer polls every 30s; logging the same row forever is noise."""
        path = tmp_path / "schedules.json"
        self._write(path, [self._poison_row()])
        store = ScheduleStore(path)

        with caplog.at_level("ERROR", logger="arcagent.modules.scheduler.store"):
            for _ in range(3):
                store.load()

        assert len([r for r in caplog.records if "sched_bad" in r.getMessage()]) == 1

    def test_malformed_json_still_raises(self, tmp_path: Path) -> None:
        """Row-level tolerance only. A corrupt FILE is a real fault and must
        reach the engine's breaker rather than look like an empty schedule set."""
        path = tmp_path / "schedules.json"
        path.write_text("{not json", encoding="utf-8")

        with pytest.raises(json.JSONDecodeError):
            ScheduleStore(path).load()
