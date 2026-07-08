"""Reciprocal Rank Fusion — the one scale-free combiner every channel shares.

Surface (vec+bm25+graph+recency), structural (trigger+cue), and the top-level
surface/structural merge all combine independently-ranked lists with RRF
(``1/(k+rank)``, k=60). It is scale-free, so scores from different channels
combine without calibration. This is the single definition of that rule.
"""

from __future__ import annotations

from collections.abc import Iterable

_RRF_K = 60


def rrf_fuse(
    ranked_lists: Iterable[list[str]], *, promoted: set[str] | None = None
) -> list[tuple[str, float]]:
    """Reciprocal-rank-fuse ranked lists into one descending ``(key, score)`` list.

    Each input list is already best-first. When ``promoted`` is given, only keys in
    that set accumulate score (the structural channel's conjunctive-gate filter).
    """
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, key in enumerate(ranked):
            if promoted is not None and key not in promoted:
                continue
            scores[key] = scores.get(key, 0.0) + 1.0 / (_RRF_K + rank)
    return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))


__all__ = ["rrf_fuse"]
