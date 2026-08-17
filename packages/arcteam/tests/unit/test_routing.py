"""Ranking published digests — the half of responder selection that needs no model.

The reported failure is the specification: six agents, one holds the NNL
technical requirements, the question names nobody. These tests pin the ranking
in isolation; the end-to-end activation test lives in arcagent.
"""

from __future__ import annotations

import pytest

from arcteam.digest import AgentDigest, DigestEntry
from arcteam.routing import (
    RRF_K,
    bm25_ranking,
    cosine_ranking,
    default_responder,
    is_ambiguous,
    prefilter,
    reciprocal_rank_fusion,
)
from arcteam.types import Channel


def _digest(handle: str, *titles: str) -> AgentDigest:
    return AgentDigest(
        agent_did=f"did:arc:{handle}",
        handle=handle,
        entries=[
            DigestEntry(artifact_id=f"{handle}-{i}", title=title, entities=_caps(title))
            for i, title in enumerate(titles)
        ],
    )


def _caps(title: str) -> list[str]:
    return [word for word in title.split() if word.isupper()]


def _fleet() -> list[AgentDigest]:
    """Six agents. Exactly one of them filed the NNL requirements."""
    return [
        _digest("brand", "Q3 brand voice guidelines", "logo usage rules"),
        _digest("sales", "pipeline review notes", "pricing sheet for Acme"),
        _digest("ops", "NNL technical requirements", "deployment runbook"),
        _digest("finance", "monthly P&L summary", "vendor invoices"),
        _digest("hr", "onboarding checklist", "leave policy"),
        _digest("legal", "master services agreement", "NDA template"),
    ]


class TestTheReportedFailure:
    def test_the_agent_holding_the_document_is_ranked_first(self) -> None:
        """No mention, no self-assessment, no model call — and the right agent wins."""
        candidates = prefilter("who has the technical requirements for NNL?", _fleet())

        assert candidates
        assert candidates[0].handle == "ops"

    def test_the_rare_token_is_what_carries_it(self) -> None:
        """BM25 is not an optimisation here; it is the mechanism that works.

        ``NNL`` appears in exactly one digest. A ranker that smeared it into a
        semantic neighbourhood would have no reason to prefer ops over sales.
        """
        candidates = prefilter("who has the technical requirements for NNL?", _fleet())

        assert "nnl" in candidates[0].matched
        assert candidates[0].lexical_rank == 1

    def test_an_unrelated_question_does_not_pick_the_same_agent(self) -> None:
        """Proof the win was retrieval and not a fixed ordering."""
        candidates = prefilter("what is our leave policy?", _fleet())

        assert candidates[0].handle == "hr"


class TestBM25Half:
    def test_a_document_sharing_no_term_earns_no_rank(self) -> None:
        ranking = bm25_ranking("NNL requirements", ["NNL requirements doc", "brand colours"])

        assert ranking == [0]

    def test_an_empty_corpus_ranks_nothing(self) -> None:
        assert bm25_ranking("anything", ["", ""]) == []


class TestDenseHalf:
    def test_it_orders_by_similarity(self) -> None:
        ranking = cosine_ranking([1.0, 0.0], [[0.0, 1.0], [0.9, 0.1], [1.0, 0.0]])

        assert ranking == [2, 1]

    def test_an_orthogonal_vector_earns_no_rank(self) -> None:
        assert cosine_ranking([1.0, 0.0], [[0.0, 1.0]]) == []


class TestFusion:
    def test_k_is_the_documented_default(self) -> None:
        assert RRF_K == 60

    def test_a_rank_contributes_one_over_k_plus_position(self) -> None:
        fused = reciprocal_rank_fusion([[7]], k=60)

        assert fused[7] == pytest.approx(1 / 61)

    def test_appearing_in_both_halves_beats_topping_one(self) -> None:
        """The whole reason to fuse: agreement outweighs a single strong opinion."""
        fused = reciprocal_rank_fusion([[0, 1], [1, 0]], k=60)
        top_of_one_only = reciprocal_rank_fusion([[2], []], k=60)

        assert fused[0] == fused[1]
        assert fused[0] > top_of_one_only[2]

    def test_the_dense_half_changes_the_answer_when_it_is_supplied(self) -> None:
        """Hybrid means hybrid — the vectors must actually move the ranking."""
        digests = [_digest("a", "quarterly revenue"), _digest("b", "annual earnings")]
        lexical_only = prefilter("earnings", digests, top_k=2)
        with_dense = prefilter(
            "earnings",
            digests,
            query_vector=[1.0, 0.0],
            digest_vectors=[[1.0, 0.0], [0.0, 1.0]],
            top_k=2,
        )

        assert lexical_only[0].handle == "b"
        assert with_dense[0].handle == "a"
        assert with_dense[0].dense_rank == 1

    def test_mismatched_vector_counts_are_ignored_rather_than_trusted(self) -> None:
        candidates = prefilter(
            "earnings",
            [_digest("a", "annual earnings")],
            query_vector=[1.0],
            digest_vectors=[[1.0], [1.0]],
        )

        assert candidates[0].dense_rank is None


class TestAmbiguity:
    def test_a_clear_winner_is_not_ambiguous(self) -> None:
        candidates = prefilter("NNL technical requirements", _fleet())

        assert not is_ambiguous(candidates)

    def test_two_agents_holding_the_same_thing_are_ambiguous(self) -> None:
        digests = [_digest("a", "NNL requirements"), _digest("b", "NNL requirements")]

        assert is_ambiguous(prefilter("NNL requirements", digests))

    def test_a_lone_candidate_needs_no_tiebreak(self) -> None:
        assert not is_ambiguous(prefilter("leave policy", _fleet(), top_k=1))


class TestDefaultResponder:
    def test_the_named_responder_answers(self) -> None:
        channel = Channel(name="work", members=["did:arc:a", "did:arc:b"], responder="did:arc:b")

        assert default_responder(channel, ["did:arc:a", "did:arc:b"]) == "did:arc:b"

    def test_a_responder_who_left_does_not_still_answer(self) -> None:
        channel = Channel(name="work", members=["did:arc:a"], responder="did:arc:gone")

        assert default_responder(channel, ["did:arc:a"]) == "did:arc:a"

    def test_an_unnamed_responder_still_resolves_to_exactly_one(self) -> None:
        """Silence is never the fallback, so an unconfigured channel still answers."""
        channel = Channel(name="work", members=["did:arc:b", "did:arc:a", "user://op"])

        assert default_responder(channel, ["did:arc:b", "did:arc:a"]) == "did:arc:a"

    def test_a_room_with_no_agents_names_nobody(self) -> None:
        channel = Channel(name="work", members=["user://op"])

        assert default_responder(channel, []) == ""
