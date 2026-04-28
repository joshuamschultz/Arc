"""Tests for TraceStore.iter_records() (SPEC-019 T2.1, T2.2).

iter_records yields one parsed dict per line across all daily files in
chronological filename order. Memory must be bounded (per-line, not per-file).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import AsyncIterator

import pytest

from arcllm.trace_store import JSONLTraceStore, TraceRecord


@pytest.fixture
def agent_root(tmp_path: Path) -> Path:
    return tmp_path / "agent_a"


@pytest.fixture
def store(agent_root: Path) -> JSONLTraceStore:
    return JSONLTraceStore(agent_root)


def _seed_daily_file(agent_root: Path, date_str: str, records: list[dict]) -> None:
    """Write JSONL records into traces-<date>.jsonl directly (bypasses chain)."""
    traces_dir = agent_root / "traces"
    traces_dir.mkdir(parents=True, exist_ok=True)
    f = traces_dir / f"traces-{date_str}.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def _record(provider: str = "p", ts: str | None = None) -> dict:
    rec = TraceRecord(
        provider=provider,
        model="m",
        timestamp=ts or datetime.now(UTC).isoformat(),
    )
    return rec.model_dump()


class TestIterRecordsEmptyStore:
    """Empty store yields nothing."""

    async def test_empty(self, store: JSONLTraceStore) -> None:
        items = [r async for r in store.iter_records()]
        assert items == []


class TestIterRecordsSingleFile:
    """All records from a single file are yielded in file order."""

    async def test_single_file(self, store: JSONLTraceStore, agent_root: Path) -> None:
        records = [_record(provider="a"), _record(provider="b"), _record(provider="c")]
        _seed_daily_file(agent_root, "2026-04-26", records)

        items = [r async for r in store.iter_records()]
        assert [r["provider"] for r in items] == ["a", "b", "c"]


class TestIterRecordsMultiFile:
    """Files are read in chronological filename order, oldest first."""

    async def test_multi_file_chronological(
        self, store: JSONLTraceStore, agent_root: Path
    ) -> None:
        _seed_daily_file(agent_root, "2026-04-25", [_record(provider="day1")])
        _seed_daily_file(agent_root, "2026-04-26", [_record(provider="day2")])
        _seed_daily_file(agent_root, "2026-04-27", [_record(provider="day3")])

        items = [r async for r in store.iter_records()]
        assert [r["provider"] for r in items] == ["day1", "day2", "day3"]


class TestIterRecordsMalformedLineTolerated:
    """Unparseable lines are skipped (logged as warning), iteration continues."""

    async def test_malformed_line_skipped(
        self, store: JSONLTraceStore, agent_root: Path
    ) -> None:
        traces_dir = agent_root / "traces"
        traces_dir.mkdir(exist_ok=True)
        f = traces_dir / "traces-2026-04-26.jsonl"
        good = json.dumps(_record(provider="ok"))
        bad = "{not json"
        also_good = json.dumps(_record(provider="ok2"))
        f.write_text(f"{good}\n{bad}\n{also_good}\n")

        items = [r async for r in store.iter_records()]
        assert [r["provider"] for r in items] == ["ok", "ok2"]


class TestIterRecordsBlankLineSkipped:
    async def test_blank_lines(
        self, store: JSONLTraceStore, agent_root: Path
    ) -> None:
        traces_dir = agent_root / "traces"
        traces_dir.mkdir(exist_ok=True)
        f = traces_dir / "traces-2026-04-26.jsonl"
        rec = json.dumps(_record(provider="ok"))
        f.write_text(f"{rec}\n\n\n")

        items = [r async for r in store.iter_records()]
        assert len(items) == 1
