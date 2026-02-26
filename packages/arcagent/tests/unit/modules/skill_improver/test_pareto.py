"""Tests for ParetoFrontier — dominance, add/evict, min-delta gate, selection."""

from __future__ import annotations

import pytest

from arcagent.modules.skill_improver.models import Candidate
from arcagent.modules.skill_improver.pareto import ParetoFrontier


def _make_candidate(
    cid: str,
    scores: dict[str, list[float]],
    aggregate: dict[str, float],
    token_count: int = 10,
    parent_id: str | None = None,
) -> Candidate:
    return Candidate(
        id=cid,
        text=f"Skill text for {cid}",
        scores=scores,
        aggregate_scores=aggregate,
        token_count=token_count,
        parent_id=parent_id,
    )


class TestPerExampleDominance:
    """E1: A >= B on all traces, > on at least one."""

    def test_dominates_all_better(self) -> None:
        f = ParetoFrontier()
        a = _make_candidate("a", {"d": [5.0, 5.0]}, {"d": 5.0}, token_count=10)
        b = _make_candidate("b", {"d": [3.0, 3.0]}, {"d": 3.0}, token_count=10)
        assert f.dominates(a, b) is True

    def test_not_dominates_equal(self) -> None:
        f = ParetoFrontier()
        a = _make_candidate("a", {"d": [3.0, 3.0]}, {"d": 3.0}, token_count=10)
        b = _make_candidate("b", {"d": [3.0, 3.0]}, {"d": 3.0}, token_count=10)
        assert f.dominates(a, b) is False

    def test_not_dominates_mixed(self) -> None:
        f = ParetoFrontier()
        a = _make_candidate("a", {"d": [5.0, 2.0]}, {"d": 3.5}, token_count=10)
        b = _make_candidate("b", {"d": [3.0, 4.0]}, {"d": 3.5}, token_count=10)
        assert f.dominates(a, b) is False

    def test_dominates_at_least_one_better(self) -> None:
        f = ParetoFrontier()
        a = _make_candidate("a", {"d": [4.0, 3.0]}, {"d": 3.5}, token_count=10)
        b = _make_candidate("b", {"d": [3.0, 3.0]}, {"d": 3.0}, token_count=10)
        assert f.dominates(a, b) is True


class TestTokenCountAsDimension:
    """E2: Token count as Pareto dimension (lower is better)."""

    def test_lower_tokens_better(self) -> None:
        f = ParetoFrontier()
        # Same scores, fewer tokens -> dominates
        a = _make_candidate("a", {"d": [4.0]}, {"d": 4.0}, token_count=5)
        b = _make_candidate("b", {"d": [4.0]}, {"d": 4.0}, token_count=15)
        assert f.dominates(a, b) is True

    def test_more_tokens_not_dominate(self) -> None:
        f = ParetoFrontier()
        a = _make_candidate("a", {"d": [4.0]}, {"d": 4.0}, token_count=20)
        b = _make_candidate("b", {"d": [4.0]}, {"d": 4.0}, token_count=10)
        assert f.dominates(a, b) is False


class TestFrontierAdd:
    """E3: Add non-dominated, evict dominated."""

    def test_add_first_candidate(self) -> None:
        f = ParetoFrontier()
        c = _make_candidate("c1", {"d": [3.0]}, {"d": 3.0})
        assert f.add(c) is True
        assert len(f.candidates) == 1

    def test_add_non_dominated(self) -> None:
        f = ParetoFrontier()
        c1 = _make_candidate("c1", {"d": [5.0, 2.0]}, {"d": 3.5}, token_count=10)
        c2 = _make_candidate("c2", {"d": [2.0, 5.0]}, {"d": 3.5}, token_count=10)
        f.add(c1)
        assert f.add(c2) is True
        assert len(f.candidates) == 2

    def test_reject_dominated(self) -> None:
        f = ParetoFrontier()
        c1 = _make_candidate("c1", {"d": [5.0, 5.0]}, {"d": 5.0}, token_count=5)
        c2 = _make_candidate("c2", {"d": [3.0, 3.0]}, {"d": 3.0}, token_count=10)
        f.add(c1)
        assert f.add(c2) is False
        assert len(f.candidates) == 1

    def test_evict_dominated(self) -> None:
        f = ParetoFrontier()
        c1 = _make_candidate("c1", {"d": [3.0, 3.0]}, {"d": 3.0}, token_count=10)
        c2 = _make_candidate("c2", {"d": [5.0, 5.0]}, {"d": 5.0}, token_count=5)
        f.add(c1)
        assert f.add(c2) is True
        assert len(f.candidates) == 1
        assert f.candidates[0].id == "c2"


class TestAddIfImproves:
    """E4: Min-delta gate over parent."""

    def test_add_with_sufficient_delta(self) -> None:
        f = ParetoFrontier()
        parent = _make_candidate("p", {"d": [3.0]}, {"d": 3.0})
        f.add(parent)
        child = _make_candidate("c", {"d": [3.5]}, {"d": 3.5}, parent_id="p")
        assert f.add_if_improves(child, min_delta=0.1) is True

    def test_reject_insufficient_delta(self) -> None:
        f = ParetoFrontier()
        parent = _make_candidate("p", {"d": [3.0]}, {"d": 3.0})
        f.add(parent)
        child = _make_candidate("c", {"d": [3.05]}, {"d": 3.05}, parent_id="p")
        assert f.add_if_improves(child, min_delta=0.1) is False

    def test_add_if_no_parent(self) -> None:
        f = ParetoFrontier()
        c = _make_candidate("c", {"d": [3.0]}, {"d": 3.0})
        assert f.add_if_improves(c, min_delta=0.1) is True


class TestFrontierSelect:
    """E5: Selection from frontier."""

    def test_select_returns_candidate(self) -> None:
        f = ParetoFrontier()
        c1 = _make_candidate("c1", {"d": [3.0]}, {"d": 3.0})
        f.add(c1)
        selected = f.select()
        assert selected.id == "c1"

    def test_select_from_multiple(self) -> None:
        f = ParetoFrontier()
        c1 = _make_candidate("c1", {"d": [5.0, 2.0]}, {"d": 3.5}, token_count=10)
        c2 = _make_candidate("c2", {"d": [2.0, 5.0]}, {"d": 3.5}, token_count=10)
        f.add(c1)
        f.add(c2)
        selected = f.select()
        assert selected.id in ("c1", "c2")

    def test_select_empty_raises(self) -> None:
        f = ParetoFrontier()
        with pytest.raises(ValueError, match="empty"):
            f.select()


class TestOverallBest:
    """E6: Highest average across dimensions."""

    def test_overall_best_single(self) -> None:
        f = ParetoFrontier()
        c = _make_candidate("c1", {"d": [4.0]}, {"d": 4.0})
        f.add(c)
        assert f.overall_best().id == "c1"

    def test_overall_best_picks_highest_average(self) -> None:
        f = ParetoFrontier()
        c1 = _make_candidate(
            "c1",
            {"acc": [4.0], "eff": [2.0]},
            {"acc": 4.0, "eff": 2.0},
            token_count=10,
        )
        c2 = _make_candidate(
            "c2",
            {"acc": [3.0], "eff": [4.0]},
            {"acc": 3.0, "eff": 4.0},
            token_count=10,
        )
        f.add(c1)
        f.add(c2)
        # c2 avg = 3.5, c1 avg = 3.0
        assert f.overall_best().id == "c2"

    def test_overall_best_empty_raises(self) -> None:
        f = ParetoFrontier()
        with pytest.raises(ValueError, match="empty"):
            f.overall_best()


class TestFrontierSerialization:
    """E7: JSON round-trip for manifest."""

    def test_to_dict_and_back(self) -> None:
        f = ParetoFrontier()
        c1 = _make_candidate("c1", {"d": [4.0]}, {"d": 4.0})
        c2 = _make_candidate("c2", {"d": [3.0, 5.0]}, {"d": 4.0}, token_count=8)
        f.add(c1)
        f.add(c2)

        data = f.to_dict()
        restored = ParetoFrontier.from_dict(data)
        assert len(restored.candidates) == 2
        ids = {c.id for c in restored.candidates}
        assert "c1" in ids
        assert "c2" in ids
