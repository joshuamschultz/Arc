"""Unit tests for arcstore.spool (SPEC-026 FR-2, Tasks 1.4/1.6/1.10/1.11)."""

from __future__ import annotations

from pathlib import Path

import arcstore.spool as spool_mod
from arcstore.records import SpoolRecord
from arcstore.spool import read, record, spool_path


def test_record_appends_durable_line_without_store(tmp_path: Path) -> None:
    # Task 1.4 — a durable line is written with only the spool imported (no backend/store).
    target = tmp_path / "operational.jsonl"
    rec = SpoolRecord(kind="llm_call", actor_did="did:a", request_id="r1", model="m", prompt_tokens=3)
    record(rec, path=target)

    assert target.exists()
    out = list(read(target))
    assert len(out) == 1
    assert out[0].actor_did == "did:a"
    assert out[0].model == "m"
    assert out[0].prompt_tokens == 3


def test_record_appends_not_overwrites(tmp_path: Path) -> None:
    target = tmp_path / "operational.jsonl"
    record(SpoolRecord(kind="run_event", actor_did="did:a", name="start"), path=target)
    record(SpoolRecord(kind="run_event", actor_did="did:a", name="finish"), path=target)
    names = [r.name for r in read(target)]
    assert names == ["start", "finish"]


def test_record_is_fail_open_on_write_error(tmp_path: Path, monkeypatch, caplog) -> None:
    # Task 1.6 — a write error is swallowed + logged; the caller proceeds (AU-5).
    def boom(*_a: object, **_k: object) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(spool_mod.os, "write", boom)
    target = tmp_path / "operational.jsonl"

    # Must not raise.
    record(SpoolRecord(kind="agent_event", actor_did="did:a"), path=target)
    assert any("swallowing (AU-5)" in r.message for r in caplog.records)


def test_record_creates_file_0600(tmp_path: Path) -> None:
    # Task 1.10 — owner-only file mode (NFR-5).
    target = tmp_path / "nested" / "operational.jsonl"
    record(SpoolRecord(kind="llm_call", actor_did="did:a"), path=target)
    assert target.exists()
    assert (target.stat().st_mode & 0o777) == 0o600


def test_read_skips_corrupt_lines(tmp_path: Path) -> None:
    target = tmp_path / "operational.jsonl"
    record(SpoolRecord(kind="llm_call", actor_did="did:a"), path=target)
    with target.open("a", encoding="utf-8") as fh:
        fh.write("{ this is not valid json\n")
    record(SpoolRecord(kind="llm_call", actor_did="did:b"), path=target)

    out = list(read(target))
    assert [r.actor_did for r in out] == ["did:a", "did:b"]


def test_read_missing_file_is_empty(tmp_path: Path) -> None:
    assert list(read(tmp_path / "nope.jsonl")) == []


def test_daily_rotation_path(tmp_path: Path) -> None:
    # Task 1.11 — path carries the date; same day → same file.
    p1 = spool_path(data_dir=tmp_path)
    p2 = spool_path(data_dir=tmp_path)
    assert p1 == p2
    assert p1.parent == tmp_path / "spool"
    assert p1.name.startswith("operational-")
    assert p1.name.endswith(".jsonl")
