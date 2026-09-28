"""SPEC-083 T-1227 (COMP-030, REQ-513) — the strict promotion-question parser.

The Jev question is an operator-editable prompt
(``arcmemory/context/promotion_classify.md``). ``parse_promotion_question``
turns its Markdown into the Choice + Noul request and refuses anything that is
not exactly the fixed shape: four fixed labels, ``Instructions`` and
``Personal check`` sections, fields ``what`` / ``not_for`` / ``examples`` only.
Every refusal is a :class:`PromotionQuestionInvalidError` (a
``ClassifierUnavailableError``), so a broken edit sends nothing rather than a
half-parsed or silently-stock question.

``question_version`` hashes the PARSED question: a whitespace-only edit keeps
the version (no needless re-classification of the whole ledger), a wording
edit changes it (every prior verdict is re-evaluated).
"""

from __future__ import annotations

import pytest
from arcprompt import PromptMissing, StockPromptSource, parse_prompt

from arcmemory.promotion.classifier import (
    PROMOTION_LABELS,
    ClassifierUnavailableError,
    PromotionQuestionInvalidError,
    question_version,
)
from arcmemory.promotion.question import (
    PROMPT_NAME,
    PROMPT_PACKAGE,
    load_promotion_question,
    parse_promotion_question,
)

#: The stock body as the agent's PromptSource returns it (frontmatter stripped).
STOCK = StockPromptSource().resolve(PROMPT_PACKAGE, PROMPT_NAME)

#: A minimal valid question, independent of the stock wording.
VALID = """\
## Instructions
Would this help the team?

## Label: company
what: Business knowledge.
not_for: Private life.
examples:
- Acme renewal terms.
- How to restart the sync job.

## Label: personal
what: The operator's private life.

## Label: agent_only
what: This assistant's own setup.

## Label: unclear
what: None of these.

## Personal check
This text is mainly about the operator's private life.
"""


def _replace(body: str, old: str, new: str) -> str:
    assert old in body, f"fixture drift: {old!r} not in body"
    return body.replace(old, new, 1)


# -- the stock file -------------------------------------------------------------


def test_stock_file_parses_to_the_v2_choice_plus_noul_question() -> None:
    question = parse_promotion_question(STOCK)

    assert set(question) == {"scope", "personal_check"}
    assert question["scope"]["type"] == "choice"
    assert question["personal_check"]["type"] == "noul"
    assert tuple(question["scope"]["criteria"]) == PROMOTION_LABELS
    # v2 wording anchors (measured 0 wrongly shared on 227 labelled memories).
    assert question["scope"]["instructions"].startswith(
        "This is one note remembered by an AI assistant that works on a company team."
    )
    assert question["scope"]["criteria"]["company"]["what"].startswith(
        "Useful to the company team:"
    )
    assert "not_for" not in question["scope"]["criteria"]["unclear"]
    assert question["personal_check"]["instructions"].startswith(
        "This text is mainly about the operator's private life"
    )


def test_stock_file_on_disk_has_frontmatter_the_source_strips() -> None:
    """The parser sees the body only; the file itself carries catalog frontmatter."""
    from arcprompt import load_stock_document

    doc = load_stock_document(PROMPT_PACKAGE, PROMPT_NAME)

    assert doc.name == PROMPT_NAME
    assert doc.body == STOCK
    assert not STOCK.lstrip().startswith("---")


def test_load_promotion_question_reads_through_the_prompt_source() -> None:
    assert question_version(load_promotion_question(StockPromptSource())) == question_version(
        parse_promotion_question(STOCK)
    )


def test_parsed_question_is_read_only() -> None:
    question = parse_promotion_question(VALID)

    with pytest.raises(TypeError):
        question["scope"]["criteria"]["company"] = {"what": "anything"}  # type: ignore[index]  # reason: proving immutability


def test_valid_question_carries_examples_and_not_for() -> None:
    company = parse_promotion_question(VALID)["scope"]["criteria"]["company"]

    assert company == {
        "what": "Business knowledge.",
        "not_for": "Private life.",
        "examples": ["Acme renewal terms.", "How to restart the sync job."],
    }


def test_a_field_value_may_wrap_onto_following_lines() -> None:
    body = _replace(VALID, "what: Business knowledge.\n", "what: Business\n  knowledge.\n")

    assert parse_promotion_question(body)["scope"]["criteria"]["company"]["what"] == (
        "Business knowledge."
    )


# -- strict rejection rules -------------------------------------------------------


_REJECTIONS: dict[str, str] = {
    "missing_label_section": _replace(VALID, "## Label: unclear\nwhat: None of these.\n\n", ""),
    "extra_label_section": _replace(
        VALID, "## Personal check", "## Label: vendor\nwhat: Vendor chatter.\n\n## Personal check"
    ),
    "duplicate_label_section": _replace(
        VALID, "## Personal check", "## Label: company\nwhat: Again.\n\n## Personal check"
    ),
    "unknown_section": _replace(
        VALID, "## Personal check", "## Notes\nfree text\n\n## Personal check"
    ),
    "missing_instructions": _replace(VALID, "## Instructions\nWould this help the team?\n\n", ""),
    "missing_personal_check": VALID.split("## Personal check")[0],
    "unknown_field": _replace(VALID, "what: The operator's", "why: nope\nwhat: The operator's"),
    "repeated_field": _replace(VALID, "not_for: Private life.\n", "not_for: A.\nnot_for: B.\n"),
    "empty_what": _replace(VALID, "what: None of these.", "what:"),
    "missing_what": _replace(VALID, "what: This assistant's own setup.", "not_for: x"),
    "empty_not_for": _replace(VALID, "not_for: Private life.", "not_for:"),
    "text_before_a_field": _replace(
        VALID,
        "what: The operator's private life.",
        "stray words\nwhat: The operator's private life.",
    ),
    "text_outside_a_section": "Preamble the model would see.\n" + VALID,
    "examples_not_a_list": _replace(VALID, "- Acme renewal terms.\n", "Acme renewal terms.\n"),
    "examples_empty": _replace(
        VALID, "- Acme renewal terms.\n- How to restart the sync job.\n", ""
    ),
    "empty_instructions": _replace(VALID, "Would this help the team?\n", ""),
    "empty_personal_check": _replace(
        VALID, "This text is mainly about the operator's private life.\n", ""
    ),
    "empty_body": "",
}


@pytest.mark.parametrize("body", list(_REJECTIONS.values()), ids=list(_REJECTIONS))
def test_strict_parser_rejects(body: str) -> None:
    with pytest.raises(PromotionQuestionInvalidError):
        parse_promotion_question(body)


def test_rejection_fixtures_are_one_edit_from_valid() -> None:
    """Narrowness: the valid base parses, so each rejection above is caused by its one edit."""
    assert parse_promotion_question(VALID)


def test_invalid_question_is_a_classifier_unavailable_error_naming_the_prompt() -> None:
    """The sweep treats it as 'send nothing tonight' and the audit names the prompt."""
    with pytest.raises(ClassifierUnavailableError) as caught:
        parse_promotion_question(_REJECTIONS["missing_personal_check"])

    assert isinstance(caught.value, PromotionQuestionInvalidError)
    assert caught.value.prompt == "arcmemory/promotion_classify"


def test_rejection_message_never_echoes_operator_text() -> None:
    marker = "SECRET-OPERATOR-WORDING-7731"
    body = _replace(
        VALID, "what: Business knowledge.", f"why: {marker}\nwhat: Business knowledge."
    )

    with pytest.raises(PromotionQuestionInvalidError) as caught:
        parse_promotion_question(body)

    assert marker not in str(caught.value)


def test_unresolvable_prompt_raises_question_invalid_not_prompt_error() -> None:
    class _Missing:
        def resolve(self, package: str, name: str) -> str:
            raise PromptMissing(package, name)

    with pytest.raises(PromotionQuestionInvalidError):
        load_promotion_question(_Missing())


# -- question_version ---------------------------------------------------------------


@pytest.mark.parametrize(
    "edit",
    [
        lambda body: body.replace("\n", "\n\n"),
        lambda body: body.replace("what: ", "what:    "),
        lambda body: body.replace("\n", "   \n"),
        lambda body: body + "\n\n\n",
    ],
    ids=["blank-lines", "spaces-after-colon", "trailing-spaces", "trailing-newlines"],
)
def test_whitespace_only_edit_keeps_question_version(edit) -> None:  # type: ignore[no-untyped-def]  # reason: parametrized lambda
    edited = edit(STOCK)
    assert edited != STOCK

    assert question_version(parse_promotion_question(edited)) == question_version(
        parse_promotion_question(STOCK)
    )


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("Would it help other people", "Would it help other teammates"),
        ("scratch notes.", "scratch notes and drafts."),
        ("hobbies).", "hobbies, pets)."),
    ],
    ids=["instructions", "agent_only-what", "personal-check"],
)
def test_wording_edit_changes_question_version(old: str, new: str) -> None:
    edited = _replace(STOCK, old, new)

    assert question_version(parse_promotion_question(edited)) != question_version(
        parse_promotion_question(STOCK)
    )


def test_adding_examples_changes_question_version() -> None:
    edited = _replace(
        STOCK,
        "not_for: The operator's private life.",
        "not_for: The operator's private life.\nexamples:\n- Acme renewal terms.",
    )

    assert question_version(parse_promotion_question(edited)) != question_version(
        parse_promotion_question(STOCK)
    )


def test_parse_prompt_round_trip_keeps_the_stock_version() -> None:
    """Resolving via a parsed document (the overlay path) yields the same version."""
    from arcprompt import render_prompt

    raw = render_prompt(STOCK, name=PROMPT_NAME, description="operator copy")
    body = parse_prompt(raw, source="overlay").body

    assert question_version(parse_promotion_question(body)) == question_version(
        parse_promotion_question(STOCK)
    )
