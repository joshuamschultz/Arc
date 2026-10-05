"""``select_candidates``: what Context prep injects, and why each candidate is or is not.

Pure selection, no I/O. Every candidate, kept or dropped, gets an explained trace
row, so an operator can read why a document did not reach the model.
"""

from __future__ import annotations

from arcagent.core.context_prep import Candidate, _Caps, estimate_tokens, select_candidates

_FOUR_CHARS_A_TOKEN = 4


def _text(tokens: int, tag: str = "t") -> str:
    """Text of exactly ``tokens`` estimated tokens, unique per ``tag``."""
    return (tag * (tokens * _FOUR_CHARS_A_TOKEN))[: tokens * _FOUR_CHARS_A_TOKEN]


def _candidate(
    source: str,
    *,
    kind: str = "memory",
    score: float = 0.5,
    tokens: int = 5,
    text: str | None = None,
    classification: str = "unclassified",
    path: str = "",
) -> Candidate:
    return Candidate(
        source_kind=kind,
        source=source,
        title=source,
        path=path,
        score=score,
        classification=classification,
        text=_text(tokens, source[:1] or "x") + source if text is None else text,
    )


def _caps(
    *,
    memory_top_k: int = 4,
    memory_tokens: int = 1000,
    docs_top_k: int = 3,
    docs_tokens: int = 1000,
    docs_floor: float = 0.0,
    memory_floor: float = 0.0,
    total_tokens: int = 10_000,
) -> _Caps:
    return _Caps(
        memory_top_k=memory_top_k,
        memory_tokens=memory_tokens,
        docs_top_k=docs_top_k,
        docs_tokens=docs_tokens,
        docs_floor=docs_floor,
        memory_floor=memory_floor,
        total_tokens=total_tokens,
    )


def _reasons(items: list[dict[str, object]]) -> dict[str, str]:
    return {str(item["source"]): str(item["reason"]) for item in items}


def test_memory_top_k_keeps_the_best_scores_and_explains_the_rest() -> None:
    cards = [_candidate(f"m{n}", score=n / 10) for n in range(1, 6)]

    chosen, items = select_candidates(cards, _caps(memory_top_k=2))

    assert [c.source for c in chosen] == ["m5", "m4"]
    reasons = _reasons(items)
    assert reasons["m5"] == reasons["m4"] == "used"
    assert reasons["m3"] == reasons["m2"] == reasons["m1"] == "below top-2"


def test_docs_top_k_is_separate_from_the_memory_top_k() -> None:
    cards = [_candidate(f"m{n}", score=0.9) for n in range(3)]
    docs = [_candidate(f"d{n}", kind="connection", score=0.9) for n in range(4)]

    chosen, _ = select_candidates([*cards, *docs], _caps(memory_top_k=3, docs_top_k=2))

    assert sum(c.source_kind == "memory" for c in chosen) == 3
    assert sum(c.source_kind == "connection" for c in chosen) == 2


def test_profile_facts_and_staged_recalls_count_against_the_memory_lane() -> None:
    cards = [
        _candidate("profile:role", kind="profile", score=1.0),
        _candidate("proactive:0", score=1.0),
        _candidate("m1", score=0.4),
    ]

    chosen, items = select_candidates(cards, _caps(memory_top_k=2))

    assert {c.source for c in chosen} == {"profile:role", "proactive:0"}
    assert _reasons(items)["m1"] == "below top-2"


def test_the_memory_token_cap_excludes_what_would_overflow_it() -> None:
    cards = [
        _candidate("small", score=0.9, tokens=10),
        _candidate("big", score=0.8, tokens=60),
    ]

    chosen, items = select_candidates(cards, _caps(memory_tokens=50))

    assert [c.source for c in chosen] == ["small"]
    assert _reasons(items)["big"] == "over the memory token cap (50)"


def test_the_docs_token_cap_does_not_spend_the_memory_budget() -> None:
    cards = [
        _candidate("m1", tokens=40),
        _candidate("d1", kind="connection", tokens=40),
        _candidate("d2", kind="connection", tokens=40, score=0.4),
    ]

    chosen, items = select_candidates(cards, _caps(memory_tokens=50, docs_tokens=50))

    assert {c.source for c in chosen} == {"m1", "d1"}
    assert _reasons(items)["d2"] == "over the connection token cap (50)"


def test_the_total_token_cap_stops_the_sum() -> None:
    cards = [
        _candidate("m1", tokens=30, score=0.9),
        _candidate("d1", kind="connection", tokens=30, score=0.9),
    ]

    chosen, items = select_candidates(cards, _caps(total_tokens=40))

    assert [c.source for c in chosen] == ["m1"]
    assert _reasons(items)["d1"] == "over the total token cap (40)"
    assert sum(c.tokens for c in chosen) <= 40


def test_documents_below_the_score_floor_are_dropped_but_memory_is_not_floored() -> None:
    cards = [
        _candidate("d-low", kind="connection", score=0.1),
        _candidate("d-high", kind="connection", score=0.7),
        _candidate("m-low", score=0.1),
    ]

    chosen, items = select_candidates(cards, _caps(docs_floor=0.3))

    assert {c.source for c in chosen} == {"d-high", "m-low"}
    assert _reasons(items)["d-low"] == "score below floor 0.3"


def test_the_same_source_or_the_same_text_collapses_onto_the_first_seen() -> None:
    cards = [
        _candidate("a", score=0.9, text="alpha body"),
        _candidate("a", score=0.8, text="alpha body, reworded"),
        _candidate("b", score=0.7, text="alpha body"),
        _candidate("c", score=0.6, text="gamma body"),
    ]

    chosen, items = select_candidates(cards, _caps())

    assert [c.source for c in chosen] == ["a", "c"]
    assert [item["reason"] for item in items] == ["used", "duplicate", "duplicate", "used"]


def test_empty_text_is_excluded_with_its_reason() -> None:
    chosen, items = select_candidates([_candidate("blank", text="  \n ")], _caps())

    assert chosen == []
    assert items[0]["included"] is False
    assert items[0]["reason"] == "empty"


def test_every_candidate_is_explained_with_a_reason() -> None:
    cards = [
        _candidate("m1"),
        _candidate("m2", text=""),
        _candidate("d1", kind="connection", score=0.0),
    ]

    chosen, items = select_candidates(cards, _caps(docs_floor=0.5))

    assert len(items) == len(cards)
    assert all(item["reason"] for item in items)
    assert {bool(item["included"]) for item in items} == {True, False}
    assert [c.source for c in chosen] == ["m1"]


def test_the_trace_row_names_provenance_and_the_token_cost() -> None:
    card = _candidate(
        "d1", kind="connection", score=0.123456, tokens=7, path="memory/connected/x/1.md"
    )

    _, (item,) = select_candidates([card], _caps())

    assert item["source_kind"] == "connection"
    assert item["path"] == "memory/connected/x/1.md"
    assert item["score"] == 0.1235
    assert item["tokens"] == estimate_tokens(card.text)


def test_a_classified_snippet_is_withheld_from_the_trace() -> None:
    secret = _candidate("s1", classification="secret", text="the launch code is 0000")
    plain = _candidate("p1", text="the lunch menu is soup")

    _, items = select_candidates([secret, plain], _caps())

    rows = {str(item["source"]): item for item in items}
    assert "launch code" not in str(rows["s1"]["snippet"])
    assert rows["s1"]["snippet"] == "[secret: withheld from the trace]"
    assert rows["p1"]["snippet"] == "the lunch menu is soup"


def test_an_unclassified_snippet_is_redacted_and_truncated() -> None:
    long_text = "word " * 100

    _, (item,) = select_candidates([_candidate("m1", text=long_text)], _caps())

    snippet = str(item["snippet"])
    assert len(snippet) <= 160
    assert snippet.endswith("…")


def test_no_candidates_select_nothing() -> None:
    assert select_candidates([], _caps()) == ([], [])


def test_a_zero_score_card_never_reaches_the_model() -> None:
    """A gibberish request still got memory cards back at score 0.0000."""
    cards = [
        Candidate("memory", "zero", "t", "", 0.0, "unclassified", "irrelevant card"),
        Candidate("memory", "weak", "t", "", 0.004, "unclassified", "weak card"),
        Candidate("memory", "good", "t", "", 0.03, "unclassified", "relevant card"),
        Candidate("connection", "doc0", "t", "d.md", 0.0, "unclassified", "zero doc"),
    ]
    chosen, items = select_candidates(cards, _caps(memory_floor=0.01))
    assert [c.source for c in chosen] == ["good"]
    reasons = _reasons(items)
    assert reasons["zero"] == "no relevance (score 0)"
    assert reasons["doc0"] == "no relevance (score 0)"
    assert reasons["weak"] == "score below floor 0.01"
