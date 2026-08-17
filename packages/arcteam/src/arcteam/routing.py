"""Ranking published digests against a question, to decide who should answer.

The decision this replaces asked each agent, separately, whether a message was
relevant to it. That is O(N) model calls where no call can see any candidate but
its own, and every one of them answered without a lookup (ADR-032). This is the
other shape: one deterministic ranking with every candidate visible at once,
costing no model call at all.

**The lexical half is not optional.** ``NNL`` is a rare proper-noun token, and
that is precisely where dense retrieval is weakest — a rare acronym is smeared
into a neighbourhood of semantically similar projects, while BM25 anchors on the
exact token. For the question that exposed the defect this module exists to fix,
BM25 is the half that finds the answer. The dense half adds recall for the
paraphrased question that shares no tokens with the document; when a deployment
has no embedder, the ranking runs lexical-only rather than failing.

The two rankings are combined with Reciprocal Rank Fusion, which needs no score
calibration between them — it reads positions, not magnitudes, so a BM25 score
of 11.4 and a cosine of 0.82 never have to be made commensurable.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from rank_bm25 import (  # type: ignore[import-untyped]  # reason: rank_bm25 ships no type stubs; we use only the documented ctor + get_scores
    BM25Plus,
)

from arcteam.digest import AgentDigest
from arcteam.types import Channel

# Cormack et al.'s published constant, and the value every later reproduction
# keeps: large enough that the top few ranks are not winner-take-all, small
# enough that rank 50 contributes almost nothing.
RRF_K = 60

_TOKEN = re.compile(r"[A-Za-z0-9_]+")


def tokenize(text: str) -> list[str]:
    """Case-folded alphanumeric tokens — the same split on both sides."""
    return [match.group(0).lower() for match in _TOKEN.finditer(text)]


@dataclass(frozen=True)
class Candidate:
    """One agent's standing in a ranking, with the evidence for it."""

    agent_did: str
    handle: str
    score: float
    lexical_rank: int | None = None
    dense_rank: int | None = None
    matched: tuple[str, ...] = field(default_factory=tuple)


def bm25_ranking(query: str, documents: list[str]) -> list[int]:
    """Document indices ordered best-first by BM25, over the documents that match.

    Sharing a query term is the entry condition, checked before scoring rather
    than inferred from it. Neither BM25 variant makes that inference safe: Okapi
    hands a *negative* weight to a term carried by more than half the corpus, so
    in a six-agent room a common word can push its own holders below zero, while
    Plus gives every document a floor above zero whether it matched or not. An
    explicit overlap test says what is meant and survives both.

    ``BM25Plus`` is the variant used, matching the team-memory search engine, and
    it is the right one here: digests are short and a lower-bounded term
    frequency stops a long digest being penalised for holding many pointers.
    """
    corpus = [tokenize(document) for document in documents]
    terms = tokenize(query)
    if not any(corpus) or not terms:
        return []
    wanted = set(terms)
    eligible = [index for index, tokens in enumerate(corpus) if wanted & set(tokens)]
    if not eligible:
        return []
    scores = BM25Plus(corpus).get_scores(terms)
    eligible.sort(key=lambda index: (-float(scores[index]), index))
    return eligible


def cosine_ranking(query_vector: list[float], vectors: list[list[float]]) -> list[int]:
    """Document indices ordered best-first by cosine similarity.

    Vectors from ``arcrun.embed_texts`` are already normalized, so this is a dot
    product; it renormalizes anyway rather than trusting a caller's embedder to
    have done it.
    """
    scored: list[tuple[int, float]] = []
    for index, vector in enumerate(vectors):
        similarity = _cosine(query_vector, vector)
        if similarity > 0.0:
            scored.append((index, similarity))
    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    return [index for index, _ in scored]


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0


def reciprocal_rank_fusion(rankings: list[list[int]], *, k: int = RRF_K) -> dict[int, float]:
    """Fuse ordered index lists into one score per index.

    Each ranking contributes ``1 / (k + position)``, so appearing in both halves
    beats topping either one alone — which is the property that makes a hybrid
    ranker more than the better of its two parts.
    """
    fused: dict[int, float] = {}
    for ranking in rankings:
        for position, index in enumerate(ranking, start=1):
            fused[index] = fused.get(index, 0.0) + 1.0 / (k + position)
    return fused


def prefilter(
    query: str,
    digests: list[AgentDigest],
    *,
    query_vector: list[float] | None = None,
    digest_vectors: list[list[float]] | None = None,
    top_k: int = 2,
    k: int = RRF_K,
) -> list[Candidate]:
    """Rank agents by what they published, best-first, at most *top_k*.

    Deterministic in its inputs, which is what lets every member of a channel
    compute the same answer independently and only the winners wake — no
    coordinator, no race, and no agent needing to see another's decision.

    The dense half runs only when both vectors are supplied; a deployment with
    no embedder gets the lexical ranking alone, which is the half that matters
    for an identifier and costs nothing.
    """
    if not digests:
        return []
    documents = [digest.as_document() for digest in digests]
    rankings = [bm25_ranking(query, documents)]
    dense: list[int] = []
    if query_vector and digest_vectors and len(digest_vectors) == len(digests):
        dense = cosine_ranking(query_vector, digest_vectors)
        rankings.append(dense)

    lexical = rankings[0]
    fused = reciprocal_rank_fusion(rankings, k=k)
    query_terms = set(tokenize(query))
    ordered = sorted(fused.items(), key=lambda pair: (-pair[1], pair[0]))

    return [
        Candidate(
            agent_did=digests[index].agent_did,
            handle=digests[index].handle,
            score=score,
            lexical_rank=_position(lexical, index),
            dense_rank=_position(dense, index),
            matched=tuple(sorted(query_terms & set(tokenize(documents[index])))),
        )
        for index, score in ordered[:top_k]
    ]


def _position(ranking: list[int], index: int) -> int | None:
    return ranking.index(index) + 1 if index in ranking else None


def is_ambiguous(candidates: list[Candidate], *, margin: float = 0.25) -> bool:
    """Whether the top two are close enough that the ranking did not decide.

    ``margin`` is a relative gap: the runner-up must trail the leader by more
    than this fraction of the leader's score for the ranking to stand on its
    own. Anything closer is handed to the router, which can compare candidates
    the way a scorer cannot.
    """
    if len(candidates) < 2:
        return False
    best, runner_up = candidates[0].score, candidates[1].score
    return best <= 0.0 or (best - runner_up) / best <= margin


def default_responder(channel: Channel, agent_dids: list[str]) -> str:
    """Who answers a message nothing else claimed. Never ``""`` when anyone can.

    The channel's named responder if it is still a member, and otherwise the
    lexicographically first agent in the room. That fallback is arbitrary but it
    is *deterministic*, which is the property that matters: every member
    computes the same name with no coordinator, so exactly one answers rather
    than none or all. An operator who dislikes the choice names one.
    """
    members = set(channel.members)
    if channel.responder and channel.responder in members:
        return channel.responder
    eligible = sorted(did for did in agent_dids if did in members)
    return eligible[0] if eligible else ""


__all__ = [
    "RRF_K",
    "Candidate",
    "bm25_ranking",
    "cosine_ranking",
    "default_responder",
    "is_ambiguous",
    "prefilter",
    "reciprocal_rank_fusion",
    "tokenize",
]
