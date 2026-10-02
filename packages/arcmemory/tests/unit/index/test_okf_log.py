"""Per-bundle ``log.md``: a deterministic change history written from the dirty set."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from arcokf import (
    LOG_DIGEST_NAME,
    LOG_NAME,
    parse_change_log,
    read_verified_log,
    validate,
)

import arcmemory.collection_index as collection_index
from arcmemory.collection_index import OkfIndexMaintainer


@pytest.fixture(autouse=True)
def _no_background_debounce(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(collection_index, "DEBOUNCE_S", 3600.0)


class _Clock:
    def __init__(self, day: str) -> None:
        self.day = day

    def __call__(self) -> date:
        return date.fromisoformat(self.day)


def _doc(root: Path, rel: str, *, title: str, body: str = "body") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntype: Note\ntitle: {title}\n---\n{body}\n", encoding="utf-8")
    return path


def _maintainer(root: Path, clock: _Clock) -> OkfIndexMaintainer:
    return OkfIndexMaintainer(root, bundle_root=False, today=clock)


def _touch(maintainer: OkfIndexMaintainer, *paths: Path) -> None:
    for path in paths:
        maintainer.mark_dirty(path)
    maintainer.drain_sync()


def test_log_md_written_newest_first_with_okf_kinds(tmp_path: Path) -> None:
    clock = _Clock("2026-10-01")
    maintainer = _maintainer(tmp_path, clock)
    first = _doc(tmp_path, "a/one.md", title="One")
    _touch(maintainer, first)
    clock.day = "2026-10-02"
    first = _doc(tmp_path, "a/one.md", title="One", body="changed")
    second = _doc(tmp_path, "b/two.md", title="Two")
    _touch(maintainer, first, second)
    clock.day = "2026-10-03"
    first.unlink()
    _touch(maintainer, first)

    entries = parse_change_log((tmp_path / LOG_NAME).read_text(encoding="utf-8"))
    assert [(e.day, e.kind, e.path) for e in entries] == [
        ("2026-10-03", "Deprecation", "a/one.md"),
        ("2026-10-02", "Update", "a/one.md"),
        ("2026-10-02", "Creation", "b/two.md"),
        ("2026-10-01", "Creation", "a/one.md"),
    ]
    assert entries[0].title == "One"  # a deprecation keeps the title it had


def test_log_md_is_validated_as_reserved(tmp_path: Path) -> None:
    maintainer = _maintainer(tmp_path, _Clock("2026-10-01"))
    _touch(maintainer, _doc(tmp_path, "x.md", title="X"))
    text = (tmp_path / LOG_NAME).read_text(encoding="utf-8")
    assert validate(text, path="log.md").valid
    assert read_verified_log(tmp_path) is not None
    # The log and its archives are never listed as documents of the bundle.
    assert "log.md" not in (tmp_path / "index.md").read_text(encoding="utf-8")


def test_log_is_stable_for_an_unchanged_tree(tmp_path: Path) -> None:
    clock = _Clock("2026-10-01")
    maintainer = _maintainer(tmp_path, clock)
    _touch(maintainer, _doc(tmp_path, "a/x.md", title="X"), _doc(tmp_path, "y.md", title="Y"))
    before = {name: (tmp_path / name).read_bytes() for name in (LOG_NAME, LOG_DIGEST_NAME)}
    clock.day = "2026-12-25"
    maintainer.sync_all()
    maintainer.sync_all(force=True)
    again = _maintainer(tmp_path, clock)
    again.sync_all(force=True)
    after = {name: (tmp_path / name).read_bytes() for name in (LOG_NAME, LOG_DIGEST_NAME)}
    assert before == after


def test_same_day_edits_collapse_to_one_line(tmp_path: Path) -> None:
    maintainer = _maintainer(tmp_path, _Clock("2026-10-01"))
    path = _doc(tmp_path, "x.md", title="X")
    _touch(maintainer, path)
    for number in range(3):
        _doc(tmp_path, "x.md", title="X", body=f"edit {number}")
        _touch(maintainer, path)
    entries = parse_change_log((tmp_path / LOG_NAME).read_text(encoding="utf-8"))
    assert [(e.kind, e.path) for e in entries] == [("Creation", "x.md")]


def test_log_is_bounded_and_rolls_into_year_archives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(collection_index, "LOG_MAX_ENTRIES", 6)
    monkeypatch.setattr(collection_index, "LOG_KEEP_ENTRIES", 4)
    clock = _Clock("2025-12-30")
    maintainer = _maintainer(tmp_path, clock)
    for number, day in enumerate(("2025-12-30", "2025-12-31", "2026-01-01", "2026-01-02")):
        clock.day = day
        _touch(
            maintainer,
            *(_doc(tmp_path, f"d{number}_{k}.md", title=f"D{number}{k}") for k in range(2)),
        )
    entries = parse_change_log((tmp_path / LOG_NAME).read_text(encoding="utf-8"))
    assert len(entries) <= 6
    assert (tmp_path / "log.2025.md").is_file()
    archived = read_verified_log(tmp_path, "log.2025.md")
    assert archived is not None and all(e.day.startswith("2025") for e in archived)
    # archives never leak into the index as documents
    assert "log.2025" not in (tmp_path / "index.md").read_text(encoding="utf-8")


def test_forged_log_is_discarded_not_merged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    clock = _Clock("2026-10-01")
    maintainer = _maintainer(tmp_path, clock)
    _touch(maintainer, _doc(tmp_path, "x.md", title="X"))
    log = tmp_path / LOG_NAME
    log.write_text(
        log.read_text(encoding="utf-8") + "- 2026-09-01 **Creation** [evil](../../etc/evil.md)\n",
        encoding="utf-8",
    )
    assert read_verified_log(tmp_path) is None
    clock.day = "2026-10-02"
    _touch(maintainer, _doc(tmp_path, "y.md", title="Y"))
    entries = read_verified_log(tmp_path)
    assert entries is not None
    assert {e.path for e in entries} == {"y.md"}
    assert "evil" not in log.read_text(encoding="utf-8")
