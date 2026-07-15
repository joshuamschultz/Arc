"""Agentic ``record_insight`` sets a real confidence and corroborates on re-record.

Regression: the agentic tool used to write ``hits=1`` with the default
``confidence=0.0`` — every agentic insight rendered "0.0%" and could never cross
the ``known`` threshold. It must now derive confidence from hits (mirroring the
pipeline) and accumulate hits when the same insight is re-seen.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB
from arcmemory.distill import confidence_from_hits
from arcmemory.stores.insight import InsightStore
from arcmemory.tools import build_memory_tools
from arcmemory.types import Confidence

_CALLER = "did:arc:default:memory/deadbeef"


def _tool(tools: list[Any], name: str) -> Any:
    return next(t for t in tools if t.name == name)


def _record_insight_tool(workspace: Path, db: MemoryDB) -> Any:
    # No pipeline configured -> the wrapper allows the write (still audited); this
    # isolates the confidence/corroboration logic under test.
    tools = build_memory_tools(
        workspace=workspace, db=db, config=MemoryConfig(), caller_did=_CALLER
    )
    return _tool(tools, "record_insight")


async def test_fresh_insight_has_nonzero_confidence(workspace: Path, db: MemoryDB) -> None:
    cfg = MemoryConfig()
    await _record_insight_tool(workspace, db).execute(
        {"id": "deal-stalls-without-champion", "statement": "s", "trigger": "t", "cues": ["c"]}
    )
    loaded = InsightStore(workspace).read("deal-stalls-without-champion")
    assert loaded is not None
    assert loaded.hits == 1
    assert loaded.confidence == confidence_from_hits(1, cfg.gamma)
    assert loaded.confidence > 0.0  # no longer the 0.0% default


async def test_re_recording_accumulates_hits_and_confidence(
    workspace: Path, db: MemoryDB
) -> None:
    cfg = MemoryConfig()
    tool = _record_insight_tool(workspace, db)
    args = {"id": "deal-stalls-without-champion", "statement": "s", "trigger": "t", "cues": ["c1"]}
    await tool.execute(args)
    # Re-seen in later windows with a new cue -> corroborates (hits grow, cues fold).
    for _ in range(3):
        await tool.execute({**args, "cues": ["c2"]})

    loaded = InsightStore(workspace).read("deal-stalls-without-champion")
    assert loaded is not None
    assert loaded.hits == 4
    assert loaded.confidence == confidence_from_hits(4, cfg.gamma)
    assert loaded.cues == ["c1", "c2"]  # non-lossy union, order-preserving
    # 4 hits crosses the default known threshold (gamma=0.536 -> ~0.88 >= 0.8).
    assert loaded.confidence >= cfg.known_threshold
    assert loaded.status is Confidence.KNOWN
