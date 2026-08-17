"""The implicit-operation trace helper: shape + real run correlation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from arcagent.utils import trace


def test_spool_auto_tool_writes_a_start_and_end_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    """One implicit op becomes two tool_events (start, end), marked implicit."""
    import arcstore.spool as spool

    captured: list[Any] = []
    monkeypatch.setattr(trace, "_spool", lambda rec: captured.append(rec))

    with spool.request_context("run-1"):
        trace.spool_auto_tool(
            "memory_search",
            actor_did="did:arc:test/agent",
            outcome="ok",
            latency_ms=12.0,
            args="what are the NNL requirements",
            result="Rust toolchain and a signed SBOM",
        )

    assert [r.phase for r in captured] == ["start", "end"]
    assert all(r.kind == "tool_event" and r.tool_name == "memory_search" for r in captured)
    assert all(r.extra.get("implicit") is True for r in captured)
    end = captured[1]
    assert end.outcome == "ok" and end.latency_ms == 12.0
    assert end.result_size == len(b"Rust toolchain and a signed SBOM")


def test_spool_auto_tool_is_a_noop_outside_a_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ambient run id → nothing is written (no orphan rows, no stray ~/.arc I/O)."""
    called = False

    def _boom(_rec: Any) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(trace, "_spool", _boom)
    trace.spool_auto_tool("memory_search", actor_did="did:arc:test/agent")
    assert called is False


def test_spool_auto_tool_inherits_the_ambient_run_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Inside a run's request_context, the pair carries that run's id — the seam
    that lands it in the right run's timeline without threading an id here."""
    import arcstore.spool as spool

    spool_file = tmp_path / "spool.jsonl"
    monkeypatch.setattr(spool, "spool_path", lambda **_: spool_file)

    with spool.request_context("run-abc"):
        trace.spool_auto_tool("memory_search", actor_did="did:arc:test/agent")

    rows = [json.loads(line) for line in spool_file.read_text().splitlines()]
    assert rows, "nothing was spooled"
    assert {r["request_id"] for r in rows} == {"run-abc"}
    assert {r["tool_name"] for r in rows} == {"memory_search"}
