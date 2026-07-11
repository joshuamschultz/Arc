"""Observe plane — arcui reads operational history from the arcstore database.

Proves the SPEC-026 FR-5 guarantee: a record written to the durable spool while
arcui was NOT running appears in arcui's reads after it starts and ingests. No
push wire involved.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from arcstore.records import SpoolRecord
from arcstore.spool import record as spool_record

from arcui.observe import Observe


def _write_audit(
    data_dir: Path, *, seq: int, actor_did: str, action: str = "gateway.fs.read"
) -> None:
    """Append one signed-chain record to the durable WORM file arcstore mirrors."""
    worm = data_dir / "worm"
    worm.mkdir(parents=True, exist_ok=True)
    line = {
        "seq": seq,
        "event_hash": f"hash-{seq}",
        "prev_hash": f"hash-{seq - 1}" if seq else "",
        "signature": "sig",
        "event": {
            "ts": f"2026-05-31T00:00:0{seq}+00:00",
            "actor_did": actor_did,
            "action": action,
            "target": "tool:x",
            "outcome": "allow",
        },
    }
    with (worm / "audit-chain.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")


def _write_call(
    data_dir: Path,
    rid: str,
    *,
    model: str = "claude",
    outcome: str = "ok",
    ts: str | None = None,
) -> None:
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
            ts=ts,
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


@pytest.mark.asyncio
async def test_stats_excludes_rows_outside_window(tmp_path: Path) -> None:
    """The ``ts_gte`` push-down drops rows older than the window cutoff.

    Two calls land now; one is stamped an hour before the 1h window opens.
    Only the two recent calls must roll up — proving the store applies the
    ``ts >= cutoff`` bound rather than counting the whole table.
    """
    now = datetime.now(UTC)
    _write_call(tmp_path, "recent-0", ts=now.isoformat())
    _write_call(tmp_path, "recent-1", ts=now.isoformat())
    _write_call(tmp_path, "stale-0", ts=(now - timedelta(hours=2)).isoformat())
    observe = Observe(data_dir=tmp_path)
    await observe.start()
    try:
        stats = await observe.stats("1h")
        assert stats["request_count"] == 2
        assert stats["total_tokens"] == 300
    finally:
        await observe.stop()


@pytest.mark.asyncio
async def test_audit_reads_worm_chain(tmp_path: Path) -> None:
    """Observe.audit surfaces the durable signed chain, filtered by actor DID."""
    _write_audit(tmp_path, seq=0, actor_did="did:arc:alpha")
    _write_audit(tmp_path, seq=1, actor_did="did:arc:beta")
    observe = Observe(data_dir=tmp_path)
    await observe.start()
    try:
        alpha = await observe.audit(agent="did:arc:alpha")
        assert [e["actor_did"] for e in alpha] == ["did:arc:alpha"]
        assert alpha[0]["action"] == "gateway.fs.read"
        assert {e["actor_did"] for e in await observe.audit()} == {
            "did:arc:alpha",
            "did:arc:beta",
        }
    finally:
        await observe.stop()


# SPEC-028 — tool/code timeline + spawn lineage + per-identity cost (FR-4)


def _spool(data_dir: Path) -> Path:
    spool = data_dir / "spool"
    spool.mkdir(parents=True, exist_ok=True)
    return spool / "operational-2026-05-31.jsonl"


def _write(data_dir: Path, rec: SpoolRecord) -> None:
    spool_record(rec, path=_spool(data_dir))


async def test_timeline_joins_on_run_id(tmp_path: Path) -> None:
    """Task 4.0 — a run's llm_call + run_event + tool_event join on request_id==run_id."""
    _write(
        tmp_path,
        SpoolRecord(
            kind="run_event",
            actor_did="did:c",
            request_id="run-1",
            ts="2026-05-31T00:00:01+00:00",
            name="turn.start",
        ),
    )
    _write(
        tmp_path,
        SpoolRecord(
            kind="tool_event",
            actor_did="did:c",
            request_id="run-1",
            ts="2026-05-31T00:00:02+00:00",
            tool_name="web.fetch",
            phase="start",
        ),
    )
    _write(
        tmp_path,
        SpoolRecord(
            kind="llm_call",
            actor_did="did:c",
            request_id="run-1",
            ts="2026-05-31T00:00:03+00:00",
            model="claude",
            outcome="ok",
        ),
    )
    observe = Observe(data_dir=tmp_path)
    await observe.start()
    try:
        timeline = await observe.timeline(run_id="run-1")
        kinds = [e["kind"] for e in timeline]
        assert kinds == ["run_event", "tool_event", "llm_call"]  # ordered by ts
        assert all(e["request_id"] == "run-1" for e in timeline)
    finally:
        await observe.stop()


async def test_runs_lists_real_runs_grouped_by_request_id(tmp_path: Path) -> None:
    """Observe.runs() returns one summary per run (request_id), newest first,
    joining run/tool/llm spool rows — not session files."""
    _write(
        tmp_path,
        SpoolRecord(
            kind="run_event",
            actor_did="did:c",
            request_id="run-1",
            agent_label="alice",
            ts="2026-05-31T00:00:01+00:00",
            name="turn.start",
        ),
    )
    _write(
        tmp_path,
        SpoolRecord(
            kind="tool_event",
            actor_did="did:c",
            request_id="run-1",
            ts="2026-05-31T00:00:02+00:00",
            tool_name="web.fetch",
            phase="start",
        ),
    )
    _write(
        tmp_path,
        SpoolRecord(
            kind="llm_call",
            actor_did="did:c",
            request_id="run-1",
            ts="2026-05-31T00:00:03+00:00",
            model="claude",
            outcome="ok",
            prompt_tokens=100,
            completion_tokens=50,
            cost_usd=0.002,
        ),
    )
    _write(
        tmp_path,
        SpoolRecord(
            kind="run_event",
            actor_did="did:c",
            request_id="run-1",
            ts="2026-05-31T00:00:04+00:00",
            name="loop.completed",
        ),
    )
    _write(
        tmp_path,
        SpoolRecord(
            kind="run_event",
            actor_did="did:c",
            request_id="run-0",
            ts="2026-05-30T00:00:01+00:00",
            name="turn.start",
        ),
    )
    observe = Observe(data_dir=tmp_path)
    await observe.start()
    try:
        runs = await observe.runs()
        assert [r["run_id"] for r in runs] == ["run-1", "run-0"]
        r = runs[0]
        assert r["agent"] == "alice"
        assert r["turns"] == 1
        assert r["tool_calls"] == 1
        assert r["llm_calls"] == 1
        assert r["total_tokens"] == 150
        assert r["status"] == "completed"
    finally:
        await observe.stop()


async def test_spawn_tree_query(tmp_path: Path) -> None:
    """Task 4.2 — Observe.spawn_tree assembles a parent→child tree from spawn_events."""
    _write(
        tmp_path,
        SpoolRecord(
            kind="spawn_event",
            actor_did="did:child1",
            parent_did="did:parent",
            child_did="did:child1",
            role="researcher",
            depth=1,
            outcome="allow",
        ),
    )
    _write(
        tmp_path,
        SpoolRecord(
            kind="spawn_event",
            actor_did="did:child2",
            parent_did="did:parent",
            child_did="did:child2",
            role="writer",
            depth=1,
            outcome="allow",
        ),
    )
    _write(
        tmp_path,
        SpoolRecord(
            kind="spawn_event",
            actor_did="did:gc",
            parent_did="did:child1",
            child_did="did:gc",
            role="helper",
            depth=2,
            outcome="allow",
        ),
    )
    observe = Observe(data_dir=tmp_path)
    await observe.start()
    try:
        tree = await observe.spawn_tree(root_did="did:parent")
        assert tree["did"] == "did:parent"
        children = {c["did"] for c in tree["children"]}
        assert children == {"did:child1", "did:child2"}
        c1 = next(c for c in tree["children"] if c["did"] == "did:child1")
        assert c1["role"] == "researcher"
        assert [g["did"] for g in c1["children"]] == ["did:gc"]
    finally:
        await observe.stop()


async def test_spawn_tree_auto_root_and_cycle_guard(tmp_path: Path) -> None:
    """EDGE-7 — root auto-detect (no root_did) + a mid-tree back-edge terminates."""
    # root → a → b, plus a malformed back-edge b → a (cycle). root never appears
    # as a child, so auto-detect resolves it; the b→a back-edge must not loop.
    _write(
        tmp_path,
        SpoolRecord(
            kind="spawn_event",
            actor_did="did:a",
            parent_did="did:root",
            child_did="did:a",
            depth=1,
            outcome="allow",
        ),
    )
    _write(
        tmp_path,
        SpoolRecord(
            kind="spawn_event",
            actor_did="did:b",
            parent_did="did:a",
            child_did="did:b",
            depth=2,
            outcome="allow",
        ),
    )
    _write(
        tmp_path,
        SpoolRecord(
            kind="spawn_event",
            actor_did="did:a",
            parent_did="did:b",
            child_did="did:a",
            depth=3,
            outcome="allow",
        ),
    )
    observe = Observe(data_dir=tmp_path)
    await observe.start()
    try:
        tree = await observe.spawn_tree()  # no root_did → auto-detect
        assert tree["did"] == "did:root"  # the only node never seen as a child
        seen: list[str] = []

        def _walk(n: dict[str, Any]) -> None:
            seen.append(n["did"])
            for c in n["children"]:
                _walk(c)

        _walk(tree)
        # root → a → b → a(cut as a childless leaf). Bounded; recursion terminated.
        assert seen == ["did:root", "did:a", "did:b", "did:a"]
        cut_leaf = tree["children"][0]["children"][0]["children"][0]
        assert cut_leaf["did"] == "did:a" and cut_leaf["children"] == []
    finally:
        await observe.stop()


def test_row_to_trace_surfaces_bodies_from_extra() -> None:
    """When raw capture is on, request/response payloads in extra are surfaced."""
    from arcui.observe import _row_to_trace

    row = {
        "record_id": "r1",
        "model": "claude-sonnet-4-6",
        "outcome": "ok",
        "extra": {
            "request_body": {"messages": [{"role": "user", "content": "hi"}]},
            "response_body": {"content": "hello"},
        },
    }
    trace = _row_to_trace(row)
    assert trace["request"]["messages"][0]["content"] == "hi"
    assert trace["response"]["content"] == "hello"
    assert trace["messages"][0]["content"] == "hi"


def test_row_to_trace_handles_json_string_extra() -> None:
    """extra may arrive as a JSON string depending on the backend read path."""
    import json as _json

    from arcui.observe import _row_to_trace

    row = {
        "record_id": "r2",
        "outcome": "ok",
        "extra": _json.dumps({"response_body": {"content": "hey"}}),
    }
    trace = _row_to_trace(row)
    assert trace["response"]["content"] == "hey"


def test_row_to_trace_metadata_only_has_no_bodies() -> None:
    """Default (no raw capture): request/response are None, nothing breaks."""
    from arcui.observe import _row_to_trace

    trace = _row_to_trace({"record_id": "r3", "outcome": "ok"})
    assert trace["request"] is None
    assert trace["response"] is None
    assert trace["messages"] is None


def test_row_to_trace_exposes_cache_token_breakdown() -> None:
    """The trace API surfaces cache read/write tokens so a consumer can compute
    hit-rate = cache_read / (input + cache_read)."""
    from arcui.observe import _row_to_trace

    row = {
        "record_id": "r4",
        "outcome": "ok",
        "prompt_tokens": 1802,
        "completion_tokens": 50,
        "cache_read_tokens": 1500,
        "cache_write_tokens": 300,
    }
    trace = _row_to_trace(row)
    assert trace["cache_read_tokens"] == 1500
    assert trace["cache_write_tokens"] == 300
    # input_tokens still carries the summed input total for existing consumers.
    assert trace["input_tokens"] == 1802


def test_row_to_trace_cache_tokens_absent_is_none() -> None:
    from arcui.observe import _row_to_trace

    trace = _row_to_trace({"record_id": "r5", "outcome": "ok"})
    assert trace["cache_read_tokens"] is None
    assert trace["cache_write_tokens"] is None
