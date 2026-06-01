"""End-to-end: code-exec + spawn surface in arcui, and survive a restart (SPEC-028 FR-4).

Seeds the durable spool exactly as arcrun/arcagent would (a code-exec tool_event,
a spawn_event lineage edge, and a child llm_call), then proves the Observe plane
renders the code timeline (UC-1), the spawn tree (UC-2), and parent-vs-child cost
separation (UC-3) — and that a fresh Observe over the same files loses nothing
(AC-4.4), because everything is read from the durable store, not a live wire.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arcstore.records import SpoolRecord
from arcstore.spool import record as spool_record

from arcui.observe import Observe

_PARENT = "did:arc:acme:agent:parent/aabbccdd"
_CHILD = "did:arc:delegate:child/deadbeef"


def _seed(data_dir: Path) -> None:
    spool = data_dir / "spool"
    spool.mkdir(parents=True, exist_ok=True)
    p = spool / "operational-2026-05-31.jsonl"
    # Parent makes an llm_call, then spawns a child. (llm_calls use auto-ts=now so
    # they fall inside the cost window regardless of when the suite runs.)
    spool_record(SpoolRecord(kind="llm_call", actor_did=_PARENT, request_id="parent-run",
                             model="claude", agent_label="parent",
                             cost_usd=0.05, prompt_tokens=200, completion_tokens=100, outcome="ok"), path=p)
    spool_record(SpoolRecord(kind="spawn_event", actor_did=_CHILD, parent_did=_PARENT,
                             child_did=_CHILD, role="researcher", depth=1, outcome="allow",
                             ts="2026-05-31T00:00:02+00:00"), path=p)
    # Child runs code, then makes its own (cheaper) llm_call under its own identity.
    spool_record(SpoolRecord(kind="tool_event", actor_did=_CHILD, request_id="child-run",
                             ts="2026-05-31T00:00:03+00:00", tool_name="execute_python",
                             phase="start", args_digest="c" * 64, args_size=42), path=p)
    spool_record(SpoolRecord(kind="tool_event", actor_did=_CHILD, request_id="child-run",
                             ts="2026-05-31T00:00:04+00:00", tool_name="execute_python",
                             phase="end", outcome="ok", latency_ms=15.0,
                             result_digest="d" * 64, result_size=8), path=p)
    spool_record(SpoolRecord(kind="llm_call", actor_did=_CHILD, request_id="child-run",
                             model="claude", agent_label="researcher:d1",
                             cost_usd=0.01, prompt_tokens=50, completion_tokens=20, outcome="ok"), path=p)


async def _assert_surfaces(observe: Observe) -> None:
    # UC-1/UC-4 — the child's code execution is visible, identifiable as code-exec.
    timeline = await observe.timeline(run_id="child-run")
    tools = [e for e in timeline if e["kind"] == "tool_event"]
    assert [t["phase"] for t in tools] == ["start", "end"]
    assert all(t["tool_name"] == "execute_python" for t in tools)
    assert tools[0]["args_digest"] == "c" * 64  # code digest present (metadata-only)

    # UC-2 — the parent→child lineage renders as a tree.
    tree = await observe.spawn_tree(root_did=_PARENT)
    assert tree["did"] == _PARENT
    assert [c["did"] for c in tree["children"]] == [_CHILD]
    assert tree["children"][0]["role"] == "researcher"

    # UC-3 — parent and child cost separated; parent total excludes the child's.
    cost = await observe.llm_by_identity("24h")
    by = {r["identity"]: r["total_cost"] for r in cost["identities"]}
    assert abs(by["parent"] - 0.05) < 1e-9
    assert abs(by["researcher:d1"] - 0.01) < 1e-9


@pytest.mark.asyncio
async def test_tool_spawn_flow_and_restart(tmp_path: Path) -> None:
    _seed(tmp_path)

    observe = Observe(data_dir=tmp_path)
    await observe.start()
    try:
        await _assert_surfaces(observe)
    finally:
        await observe.stop()

    # AC-4.4 — a fresh Observe (server restart) re-reads the durable store and
    # loses no tool/spawn/cost history. No live wire was ever involved.
    restarted = Observe(data_dir=tmp_path)
    await restarted.start()
    try:
        await _assert_surfaces(restarted)
    finally:
        await restarted.stop()
