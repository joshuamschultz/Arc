from __future__ import annotations

from pathlib import Path

import pytest
from arcokf import (
    LOG_DIGEST_NAME,
    LOG_NAME,
    ChangeLogError,
    LogEntry,
    listable_file,
    merge_log_events,
    parse_change_log,
    read_verified_log,
    render_change_log,
    render_log_digest,
    validate,
)


def _entry(day: str, kind: str, path: str, title: str = "T", summary: str = "") -> LogEntry:
    return LogEntry(day, kind, path, title, summary)


def test_log_md_is_validated_as_reserved() -> None:
    text = render_change_log([_entry("2026-10-02", "Creation", "a/b.md", "B", "first")])
    assert validate(text, path="log.md").valid
    # A reserved file may not masquerade as a concept document.
    forged = "---\ntype: Entity\n---\n" + text
    assert not validate(forged, path="log.md").valid
    with pytest.raises(ChangeLogError):
        parse_change_log(forged)


def test_log_renders_newest_first_with_okf_kinds() -> None:
    text = render_change_log(
        [
            _entry("2026-09-30", "Deprecation", "old.md"),
            _entry("2026-10-02", "Creation", "new.md"),
            _entry("2026-10-01", "Update", "mid.md"),
        ]
    )
    days = [line.split()[1] for line in text.splitlines() if line.startswith("- ")]
    assert days == ["2026-10-02", "2026-10-01", "2026-09-30"]
    assert "**Creation**" in text and "**Update**" in text and "**Deprecation**" in text
    assert parse_change_log(text)[0].path == "new.md"


def test_render_is_stable() -> None:
    entries = [_entry("2026-10-02", "Creation", "b.md"), _entry("2026-10-02", "Update", "a.md")]
    assert render_change_log(entries) == render_change_log(list(reversed(entries)))


@pytest.mark.parametrize(
    "path", ["../x.md", "/etc/x.md", "a/../../x.md", "a\\b.md", "log.md", "x.txt", "a//b.md"]
)
def test_unsafe_paths_are_refused(path: str) -> None:
    with pytest.raises(ChangeLogError):
        render_change_log([_entry("2026-10-02", "Creation", path)])


def test_forged_lines_are_rejected() -> None:
    text = render_change_log([_entry("2026-10-02", "Creation", "a.md")])
    for forged in (
        text + "- 2026-10-03 **Creation** [x](../../etc/passwd.md)\n",
        text + "stray prose\n",
        text.replace("**Creation**", "**Deletion**"),
        text + "- 2026-10-04 **Update** [a](a.md)\n",  # out of order
    ):
        with pytest.raises(ChangeLogError):
            parse_change_log(forged)


def test_same_day_changes_collapse() -> None:
    merged = merge_log_events(
        [_entry("2026-10-02", "Creation", "a.md", "A")],
        [_entry("2026-10-02", "Update", "a.md", "A2"), _entry("2026-10-02", "Update", "b.md")],
    )
    kinds = {e.path: e.kind for e in merged}
    assert kinds == {"a.md": "Creation", "b.md": "Update"}
    deleted = merge_log_events(merged, [_entry("2026-10-02", "Deprecation", "a.md")])
    assert {e.path: e.kind for e in deleted}["a.md"] == "Deprecation"


def test_archive_names_are_reserved_not_listable() -> None:
    assert not listable_file("log.2026.md")
    assert not listable_file(LOG_NAME)
    assert listable_file("logbook.md")


def test_read_verified_log_fails_closed_on_edit(tmp_path: Path) -> None:
    text = render_change_log([_entry("2026-10-02", "Creation", "a.md")])
    (tmp_path / LOG_NAME).write_text(text, encoding="utf-8")
    assert read_verified_log(tmp_path) is None  # no sidecar: never trusted
    (tmp_path / LOG_DIGEST_NAME).write_text(render_log_digest(text, {}), encoding="utf-8")
    assert read_verified_log(tmp_path) is not None
    forged = render_change_log(
        [_entry("2026-10-02", "Creation", "a.md"), _entry("2026-10-01", "Update", "evil.md")]
    )
    (tmp_path / LOG_NAME).write_text(forged, encoding="utf-8")
    assert read_verified_log(tmp_path) is None
