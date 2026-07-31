"""The vendored reference judge prompts, pinned — COMP-011 / REQ-189.

Because CON-3 routes grading through our own arcllm judge, the reference
`print_qa_metrics.py` — which hard-asserts the judge model string — cannot
consume our output. Prompt fidelity is the whole of what is left of
comparability with the published numbers, so every template's bytes are pinned
by digest here. A digest that no longer matches means someone edited vendored
text, and the build stops.

The digests were taken from
`src/evaluation/evaluate_qa.py` at revision
`d6dc8b50a2d9ac0c99485ea28fa5755c62414c34` of
https://github.com/xiaowu0162/LongMemEval, parsed out of the function's
`if`/`elif` chain rather than transcribed. They live here rather than beside the
strings so that the text and its guard are two files: one careless edit cannot
move both.

Nothing here makes a network call, starts an agent, or writes anything.
"""

from __future__ import annotations

import hashlib

import pytest

from evaluations.longmemeval import reference_prompts as rp

# sha256 of each template's UTF-8 bytes, as fetched from the reference repo.
PINNED_DIGESTS = {
    "STANDARD": "fba020ba3d57982efdc9a937c1c01f897b789a608c7f88e60244121f6505e5bc",
    "TEMPORAL_REASONING": "8d33a5fdd83afeeb4592454a965eab43d1fcb2dedc042d1d3892f4254be6c273",
    "KNOWLEDGE_UPDATE": "183a9b3a6197ec620940f610cdc1207201ec98c1113dd633ea685cfc322fafac",
    "SINGLE_SESSION_PREFERENCE": (
        "741ee3bcbea7ff5e8ed359acef61d2f8ded3de021bbcff6ee13de455f2e2aa9b"
    ),
    "ABSTENTION": "5c0b365a1e1d06db36377c735432b56e122ca3c428f89faf61d43a0d5a7e050b",
}

# The reference's own `if`/`elif` chain, restated as data.
SELECTION = {
    "single-session-user": "STANDARD",
    "single-session-assistant": "STANDARD",
    "multi-session": "STANDARD",
    "temporal-reasoning": "TEMPORAL_REASONING",
    "knowledge-update": "KNOWLEDGE_UPDATE",
    "single-session-preference": "SINGLE_SESSION_PREFERENCE",
}


@pytest.mark.parametrize(("name", "digest"), sorted(PINNED_DIGESTS.items()))
def test_template_bytes_match_the_vendored_digest(name: str, digest: str) -> None:
    template: str = getattr(rp, name)
    actual = hashlib.sha256(template.encode("utf-8")).hexdigest()
    assert actual == digest, (
        f"{name} has drifted from the vendored reference prompt.\n"
        f"  expected sha256 {digest}\n"
        f"  actual   sha256 {actual}\n"
        f"  text: {template!r}\n"
        "Restore it from evaluate_qa.py at the revision named in "
        "evaluations/longmemeval/reference_prompts.py — never hand-edit it."
    )


@pytest.mark.parametrize(("question_type", "expected"), sorted(SELECTION.items()))
def test_selection_matches_the_reference_branch(question_type: str, expected: str) -> None:
    assert rp.reference_template(question_type) is getattr(rp, expected)


@pytest.mark.parametrize("question_type", sorted(SELECTION))
def test_abstention_overrides_every_base_type(question_type: str) -> None:
    """The reference's abstention branch is the OUTER `if` — it wins outright."""
    assert rp.reference_template(question_type, is_abstention=True) is rp.ABSTENTION


def test_unknown_question_type_is_refused_not_defaulted() -> None:
    """Falling back to the standard grader would score a type with the wrong rubric."""
    with pytest.raises(rp.UnknownQuestionTypeError):
        rp.reference_template("single-session-preferences")


def test_the_six_benchmark_types_are_all_covered() -> None:
    assert set(rp.TEMPLATES_BY_TYPE) == set(SELECTION)


def test_the_table_cannot_be_extended_at_runtime() -> None:
    with pytest.raises(TypeError):
        rp.TEMPLATES_BY_TYPE["invented-type"] = rp.STANDARD  # type: ignore[index]  # read-only by construction — that is what this asserts


@pytest.mark.parametrize("name", sorted(PINNED_DIGESTS))
def test_every_template_takes_exactly_three_positional_slots(name: str) -> None:
    """`{}` slots, filled positionally as question, gold, response — the reference's order."""
    template: str = getattr(rp, name)
    assert template.count("{}") == 3
    assert "{0}" not in template and "{question}" not in template


def test_reference_prompt_fills_slots_in_the_reference_order() -> None:
    rendered = rp.reference_prompt(
        question_type="multi-session",
        question="Q-TEXT",
        answer="GOLD-TEXT",
        response="MODEL-TEXT",
    )
    assert rendered == rp.STANDARD.format("Q-TEXT", "GOLD-TEXT", "MODEL-TEXT")
    assert rendered.index("Q-TEXT") < rendered.index("GOLD-TEXT") < rendered.index("MODEL-TEXT")


def test_the_preference_template_labels_the_gold_field_a_rubric() -> None:
    """REQ-191 — the dataset `answer` for this type IS a rubric, and is graded as one."""
    assert "\n\nRubric: {}\n\n" in rp.SINGLE_SESSION_PREFERENCE
    assert "Correct Answer:" not in rp.SINGLE_SESSION_PREFERENCE


def test_the_abstention_template_labels_the_gold_field_an_explanation() -> None:
    assert "\n\nExplanation: {}\n\n" in rp.ABSTENTION
    assert "Correct Answer:" not in rp.ABSTENTION


def test_the_reference_whitespace_quirk_is_preserved() -> None:
    """Two templates carry a trailing space before `\\n\\nQuestion:` and three do not.

    It is a typo in the reference. Tidying it would change the bytes the
    published numbers were produced with, which is exactly what REQ-189 forbids.
    """
    assert rp.STANDARD.count("answer no. \n\nQuestion:") == 1
    assert rp.TEMPORAL_REASONING.count("still correct. \n\nQuestion:") == 1
    for tidy in (rp.KNOWLEDGE_UPDATE, rp.SINGLE_SESSION_PREFERENCE, rp.ABSTENTION):
        assert " \n\nQuestion:" not in tidy
