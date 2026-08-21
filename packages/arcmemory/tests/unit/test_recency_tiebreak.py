"""RED — recency breaks exact relevance ties in fusion (SPEC-072 COMP-010).

When two candidates score EQUALLY on relevance, the more recent ranks higher — a cheap
deterministic tie-break (REQ-360). A real relevance gap is never overridden by recency.
Exercised directly on ``_rrf_fuse`` (the fuse/rank step, before the bound).
"""

from __future__ import annotations

from arcmemory.retrieve import _rrf_fuse
from arcmemory.types import Recall


def test_recency_breaks_an_exact_relevance_tie() -> None:
    # Sole item of its own channel at rank 0 → identical RRF score. The older source
    # sorts FIRST alphabetically, so only a recency tie-break can put the newer on top.
    older = Recall(source="aaa-old", content="x", score=0.0, established="2020-01-01")
    newer = Recall(source="zzz-new", content="y", score=0.0, established="2026-01-01")

    fused = _rrf_fuse([[older], [newer]])

    assert [r.source for r in fused] == ["zzz-new", "aaa-old"]


def test_a_real_relevance_gap_is_not_overridden_by_recency() -> None:
    # 'strong' appears in BOTH channels (higher fused score) but is OLD; 'weak' is newer
    # but appears once. Relevance must win — recency only breaks exact ties.
    strong_old = Recall(source="aaa-strong", content="x", score=0.0, established="2000-01-01")
    weak_new = Recall(source="zzz-weak", content="y", score=0.0, established="2030-01-01")

    fused = _rrf_fuse([[strong_old, weak_new], [strong_old]])

    assert fused[0].source == "aaa-strong"


def test_unstamped_loses_a_tie_to_a_stamped_card() -> None:
    stamped = Recall(source="zzz-stamped", content="x", score=0.0, established="2025-05-05")
    unstamped = Recall(source="aaa-unstamped", content="y", score=0.0, established="")

    fused = _rrf_fuse([[stamped], [unstamped]])

    assert fused[0].source == "zzz-stamped"
