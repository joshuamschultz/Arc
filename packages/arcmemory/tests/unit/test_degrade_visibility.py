"""The semantic channel must never go dark *silently* (the silent-degrade trap).

arcmemory is designed to degrade — no embedder means BM25 + graph still answer and
nothing raises (REQ-041). The defect this suite pins is that the degrade was
*invisible*: an operator running the default ``embed_backend = "local"`` with the
embedder package absent got a fully-working-looking recall that had lost its
semantic third, discoverable only by reading an audit log.

So: the degrade must be LOUD (a structured warning an operator sees), exactly ONCE
per process per reason (a per-query flood is its own bug), and it must not change
the fallback — recall still returns results, and still never raises.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from arcmemory.db import MemoryDB
from arcmemory.degrade import reset_degrade_warnings, semantic_degraded, warn_once
from arcmemory.index.rebuild import EmbeddingUnavailableError, embed_or_none
from arcmemory.index.surface import SurfaceIndex
from arcmemory.stores.episodic import EpisodicStore
from arcmemory.types import Event, Scope


class UnavailableEmbedder:
    """A *wired* embedder whose backend cannot serve (the arcllm 'none' signal)."""

    def __init__(self) -> None:
        self.calls = 0

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        raise EmbeddingUnavailableError("the arcllm[local] extra is not installed")


@pytest.fixture(autouse=True)
def _fresh_warnings() -> None:
    """Each test starts with an un-warned process (the flag is process-global)."""
    reset_degrade_warnings()


def _seed(db: MemoryDB, workspace: Path, scope: Scope) -> None:
    episodic = EpisodicStore(db, workspace)
    for i, text in enumerate(
        ["the dog barked at the mailman", "the cat meowed", "the sedan needs an engine"]
    ):
        episodic.append(
            Event(
                event_id=f"e{i}",
                ts=f"2026-01-0{i + 1}T00:00:00+00:00",
                scope=scope.key,
                kind="obs",
                text=text,
            )
        )


# -- the funnel warns, loudly, once ----------------------------------------


async def test_embed_or_none_warns_once_when_embedder_is_not_wired(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No embedder at all -> one WARNING naming the dead channel, not one per call."""
    with caplog.at_level(logging.WARNING, logger="arcmemory.degrade"):
        for _ in range(5):
            assert await embed_or_none(None, ["anything"]) is None

    records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(records) == 1
    assert "semantic" in records[0].getMessage().lower()


async def test_embed_or_none_warns_once_when_wired_embedder_is_unavailable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A wired-but-dead embedder warns once and still degrades to ``None``."""
    embedder = UnavailableEmbedder()
    with caplog.at_level(logging.WARNING, logger="arcmemory.degrade"):
        for _ in range(5):
            assert await embed_or_none(embedder, ["anything"]) is None

    records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(records) == 1
    assert embedder.calls == 5  # the seam is still tried every time


async def test_the_two_degrade_reasons_warn_independently(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """"Never wired" and "wired but dead" are different operator problems."""
    with caplog.at_level(logging.WARNING, logger="arcmemory.degrade"):
        await embed_or_none(None, ["x"])
        await embed_or_none(UnavailableEmbedder(), ["x"])

    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 2


def test_warn_once_is_keyed_and_resettable(caplog: pytest.LogCaptureFixture) -> None:
    """The once-per-process gate is per key, and ``reset`` re-arms it (test seam)."""
    with caplog.at_level(logging.WARNING, logger="arcmemory.degrade"):
        assert warn_once("k", "first") is True
        assert warn_once("k", "second") is False
        reset_degrade_warnings()
        assert warn_once("k", "third") is True

    assert len(caplog.records) == 2


def test_semantic_degraded_reports_whether_the_channel_went_dark() -> None:
    """The process-wide flag an operator/CLI can read without parsing logs."""
    assert semantic_degraded() is False
    warn_once("embedder:not-wired", "gone")
    assert semantic_degraded() is True


# -- the fallback stays intact ---------------------------------------------


async def test_surface_search_still_answers_without_an_embedder(
    db: MemoryDB, workspace: Path, scope: Scope
) -> None:
    """BM25 + graph + recency must still return results; recall never raises."""
    _seed(db, workspace, scope)
    index = SurfaceIndex(db, workspace, scope, embedder=None)
    await index.index_if_needed()

    result = await index.search("dog", top_k=3)

    assert result.degraded is True
    assert result.recalls, "recall must still answer on BM25 + graph"
    assert any("dog" in r.content for r in result.recalls)


async def test_surface_search_warns_once_across_many_queries(
    db: MemoryDB, workspace: Path, scope: Scope, caplog: pytest.LogCaptureFixture
) -> None:
    """A per-query log flood is its own bug — twenty searches, one warning."""
    _seed(db, workspace, scope)
    index = SurfaceIndex(db, workspace, scope, embedder=None)
    await index.index_if_needed()

    with caplog.at_level(logging.WARNING, logger="arcmemory.degrade"):
        for _ in range(20):
            await index.search("dog", top_k=3)

    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1
