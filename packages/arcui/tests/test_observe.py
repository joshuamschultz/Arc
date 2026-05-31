"""Observe plane — arcui reads operational history from the arcstore database.

Proves the SPEC-026 FR-5 guarantee: a record written to the durable spool while
arcui was NOT running appears in arcui's reads after it starts and ingests. No
push wire involved.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arcstore.records import SpoolRecord
from arcstore.spool import record as spool_record

from arcui.observe import Observe


def _write_call(data_dir: Path, rid: str, *, model: str = "claude", outcome: str = "ok") -> None:
    spool = data_dir / "spool"
    spool.mkdir(parents=True, exist_ok=True)
    spool_record(
        SpoolRecord(
            kind="llm_call",
            actor_did="did:arc:acme:analyst/aabbccdd",
            request_id=rid,
            model=model,
            prompt_tokens=100,
            completion_tokens=50,
            cost_usd=0.0015,
            latency_ms=42.0,
            outcome=outcome,
        ),
        path=spool / "operational-2026-05-31.jsonl",
    )


@pytest.mark.asyncio
async def test_reads_offline_written_calls(tmp_path: Path) -> None:
    # Calls written to the durable spool BEFORE arcui starts.
    _write_call(tmp_path, "r0")
    _write_call(tmp_path, "r1")

    observe = Observe(data_dir=tmp_path)
    await observe.start()
    try:
        traces = await observe.traces(limit=10)
        assert len(traces) == 2
        t = traces[0]
        assert t["model"] == "claude"
        assert t["total_tokens"] == 150
        assert t["duration_ms"] == 42.0
        assert t["agent"] == "did:arc:acme:analyst/aabbccdd"
        assert t["trace_id"]

        single = await observe.trace(t["trace_id"])
        assert single is not None and single["trace_id"] == t["trace_id"]
    finally:
        await observe.stop()


@pytest.mark.asyncio
async def test_stats_rolls_up_from_store(tmp_path: Path) -> None:
    for i in range(3):
        _write_call(tmp_path, f"r{i}")
    observe = Observe(data_dir=tmp_path)
    await observe.start()
    try:
        stats = await observe.stats("24h")
        assert stats["request_count"] == 3
        assert stats["total_tokens"] == 450
        assert round(stats["total_cost"], 4) == 0.0045
        assert stats["model_stats"]["claude"]["request_count"] == 3
        assert stats["latency_avg"] == 42.0
    finally:
        await observe.stop()
