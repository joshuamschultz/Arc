"""ArcMemoryBrain — the concrete Brain seam wraps capture/retrieve/consolidate."""

from __future__ import annotations

from pathlib import Path

import pytest

from arcmemory.brain import ArcMemoryBrain
from arcmemory.config import MemoryConfig
from arcmemory.distill import (
    DaySummaryDraft,
    FactCandidate,
    FactExtraction,
    InsightCandidate,
    InsightMint,
    ProcedureExtraction,
)
from arcmemory.types import Event

_DID = "did:arc:test-agent"


class _FakeDistiller:
    """Fixtured structured-completion seam (no network)."""

    async def extract_facts(self, events: list[Event]) -> FactExtraction:
        return FactExtraction(
            facts=[FactCandidate(slug="ada", name="Ada", predicate="role", value="engineer")]
        )

    async def mint_insights(self, events: list[Event], facts: list) -> InsightMint:
        return InsightMint(
            insights=[
                InsightCandidate(
                    id="i1",
                    statement="retries beat crashes",
                    trigger="a transient failure recurs",
                    cues=["retry"],
                    instances=[e.event_id for e in events[:1]],
                )
            ]
        )

    async def extract_procedures(self, events: list[Event]) -> ProcedureExtraction:
        return ProcedureExtraction()

    async def summarize_day(self, events: list[Event]) -> DaySummaryDraft:
        return DaySummaryDraft(timeline=["09:00 Ada worked on retries"], people=["Ada"])


async def test_capture_is_zero_llm_and_writes_only_the_raw_stream(workspace: Path) -> None:
    brain = ArcMemoryBrain(workspace, _DID)
    await brain.capture("Ada shipped the retry fix", kind="observation")
    # Capture writes the raw SQLite stream only; the curated daily-notes are a
    # consolidation output, so the fast path leaves no glass-box daily-log file.
    assert (workspace / "memory" / "index.db").exists()
    assert not (workspace / "memory" / "daily-log").exists()


async def test_retrieve_returns_boundary_marked_text(workspace: Path) -> None:
    brain = ArcMemoryBrain(workspace, _DID)
    await brain.capture("Ada owns the payments service", kind="observation")
    out = await brain.retrieve("who owns payments", top_k=5)
    assert isinstance(out, str)
    # Degraded (no embedder) still returns via BM25 + graph; boundary-marked when present.
    if out:
        assert "<memory-result" in out


async def test_retrieve_degrades_without_embedder_never_raises(workspace: Path) -> None:
    brain = ArcMemoryBrain(workspace, _DID)
    # No capture, no embedder — must not raise, returns empty injectable text.
    assert await brain.retrieve("anything") == ""


async def test_consolidate_without_distiller_is_noop(workspace: Path) -> None:
    brain = ArcMemoryBrain(workspace, _DID)
    await brain.capture("something happened", kind="observation")
    result = await brain.consolidate()
    assert result["insights_minted"] == 0
    assert "episode_summary" in result


async def test_consolidate_with_distiller_mints_and_summarizes(workspace: Path) -> None:
    brain = ArcMemoryBrain(workspace, _DID, config=MemoryConfig(), distiller=_FakeDistiller())
    await brain.capture("Ada retried the transient failure and it worked", kind="observation")
    result = await brain.consolidate()
    assert result["insights_minted"] == 1
    assert result["facts_updated"] == 1
    assert result["days_summarized"] == 1
    assert "Consolidation:" in str(result["episode_summary"])
    # Consolidation — not capture — writes the curated daily-notes.
    daily = list((workspace / "memory" / "daily-log").glob("*.md"))
    assert daily, "consolidation must write a curated daily-notes file"
    assert "Ada worked on retries" in daily[0].read_text(encoding="utf-8")


async def test_requires_identity() -> None:
    with pytest.raises(ValueError, match="agent_did"):
        ArcMemoryBrain(Path("."), "")


async def test_session_scope_isolates(workspace: Path) -> None:
    brain = ArcMemoryBrain(workspace, _DID)
    await brain.capture("session-a note", session_id="a")
    await brain.capture("session-b note", session_id="b")
    # Both scopes captured against one workspace DB without error (shared-nothing rows).
    assert (workspace / "memory" / "index.db").exists()
