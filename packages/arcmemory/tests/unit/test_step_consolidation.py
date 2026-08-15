"""Consolidating a merged procedure's steps must never lose an instruction.

Folding duplicate procedure cards is a union, so eleven differently-worded
versions of one method produced a faithful, unusable 54-step card (median across
that store: 6). Collapsing the repetition is the point — but "consolidate" and
"silently drop the operator's method" are the same LLM call unless the result is
checked.

So every rewritten step declares which input steps it covers, and the rewrite is
accepted only when every input is accounted for exactly once. Anything else keeps
the union: a long procedure is a nuisance, a quietly shortened one is a procedure
that no longer does what its author wrote.
"""

from __future__ import annotations

from arcmemory.stores.procedural import apply_consolidated_steps
from arcmemory.types import Step

_STEPS = [
    Step(text="check the index first", hits=2),
    Step(text="check ai_theses/README.md before anything", hits=1),
    Step(text="verify the write with a read-back", hits=3),
]


def test_a_covering_rewrite_is_accepted() -> None:
    """The happy path: two ways of saying one thing become one step."""
    rewrite = [
        ("Check the ai_theses/README.md index first", [1, 2]),
        ("Verify the write with a read-back", [3]),
    ]

    result = apply_consolidated_steps(_STEPS, rewrite)

    assert result is not None
    assert [s.text for s in result] == [
        "Check the ai_theses/README.md index first",
        "Verify the write with a read-back",
    ]


def test_evidence_follows_the_steps_it_merged() -> None:
    """A consolidated step is as corroborated as everything it absorbed.

    Otherwise consolidating a well-evidenced method resets it to a first sighting,
    and the card looks less trustworthy for having been tidied.
    """
    rewrite = [("Check the index first", [1, 2]), ("Verify the write", [3])]

    result = apply_consolidated_steps(_STEPS, rewrite)

    assert result is not None
    assert result[0].hits == 3, "the merged step lost the evidence of both originals"
    assert result[1].hits == 3


def test_a_rewrite_that_drops_a_step_is_refused() -> None:
    """The failure that matters: an instruction quietly disappearing."""
    rewrite = [("Check the index first", [1, 2])]  # never covers 3

    assert apply_consolidated_steps(_STEPS, rewrite) is None


def test_a_rewrite_that_double_counts_a_step_is_refused() -> None:
    """Covering one input twice means the model lost track, so trust nothing in it."""
    rewrite = [("Check the index", [1, 2]), ("Check it again", [2, 3])]

    assert apply_consolidated_steps(_STEPS, rewrite) is None


def test_a_rewrite_naming_a_step_that_does_not_exist_is_refused() -> None:
    """An out-of-range number is a hallucinated citation — the rest is unreliable too."""
    rewrite = [("Check the index", [1, 2]), ("Verify", [3, 9])]

    assert apply_consolidated_steps(_STEPS, rewrite) is None


def test_an_empty_rewrite_is_refused() -> None:
    """A provider hiccup must leave the method intact, not erase it."""
    assert apply_consolidated_steps(_STEPS, []) is None


def test_a_rewrite_that_saves_nothing_is_refused() -> None:
    """If it did not actually collapse anything, keep the original wording.

    Accepting a same-length rewrite would let the model quietly reword the
    operator's method for no benefit, on every consolidation pass.
    """
    rewrite = [(s.text, [i]) for i, s in enumerate(_STEPS, start=1)]

    assert apply_consolidated_steps(_STEPS, rewrite) is None
