"""T-1193 (SPEC-083 COMP-013) — the ``PromotionClassifier`` seam contract.

RED intent: ``arcmemory.promotion.classifier`` does not exist yet, so the import
fails with ``ModuleNotFoundError`` — the feature is absent.

Contract under test (SDD COMP-013, README decisions 4 and 5):

- ``ClassifierInput(item_kind, item_id, content)`` — content only; no DID, agent
  name or workspace path can ride along. ``item_kind`` is one of
  ``insight|procedure|entity`` and is validated at runtime (episodic/daily are
  never sendable).
- ``ClassifierVerdict(label, confidence, probabilities, personal_probability,
  classifier_id, classifier_version, request_id, input_tokens)`` — immutable.
- ``PromotionClassifier`` Protocol: ``classifier_id``, ``question_version``,
  ``async classify(item) -> ClassifierVerdict``.
- ``ClassifierUnavailableError`` and ``ClassifierCallError`` — distinct typed errors.
- ``PROMOTION_QUESTION`` — the canonical Choice + Noul question from the SDD.
- ``question_version(question) -> str`` ==
  ``"sha256:" + sha256(canonical_json(question)).hexdigest()``: stable for the
  canonical question, different when ANY option text changes.
"""

from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import Mapping
from typing import Any

import pytest
from arcprompt import StockPromptSource
from arctrust import canonical_json

from arcmemory.promotion.classifier import (
    ClassifierCallError,
    ClassifierInput,
    ClassifierUnavailableError,
    ClassifierVerdict,
    PromotionClassifier,
    question_version,
)
from arcmemory.promotion.question import load_promotion_question

#: The packaged stock question (arcmemory/context/promotion_classify.md), parsed.
PROMOTION_QUESTION = load_promotion_question(StockPromptSource())

_LABELS = {"company", "personal", "agent_only", "unclear"}

_CANONICAL_QUESTION = {
    "scope": {
        "type": "choice",
        "instructions": (
            "This is one note remembered by an AI assistant that works on a company team. "
            "Would it help other people or other AI assistants on the same company team?"
        ),
        "criteria": {
            "company": {
                "what": (
                    "Useful to the company team: people, organizations, clients, partners, "
                    "vendors, products, tools and systems the business uses or builds; deals "
                    "(terms, pricing, counterparties); market and competitor information; and "
                    "procedures or working methods any teammate or assistant could reuse, such "
                    "as how to follow up on commitments, diagnose a tool failure, or delegate work."
                ),
                "not_for": "The operator's private life.",
            },
            "personal": {
                "what": (
                    "The operator's private life: family, health, home, personal money, "
                    "personal appointments, hobbies, and side projects unrelated to the business."
                ),
                "not_for": "Business contacts, company work, or general working methods.",
            },
            "agent_only": {
                "what": (
                    "Only meaningful to this one assistant's own setup and would confuse others: "
                    "its private configuration, identity files, one-off session state, scratch notes."
                ),
                "not_for": "General working methods other assistants could also follow.",
            },
            "unclear": {
                "what": (
                    "None of these, not enough information to tell, or text that is not a "
                    "statement about work or personal life."
                ),
            },
        },
    },
    "personal_check": {
        "type": "noul",
        "instructions": (
            "This text is mainly about the operator's private life "
            "(family, health, home, personal money, hobbies)."
        ),
    },
}


def _plain(value: Any) -> Any:
    """Deep-copy any (possibly read-only) mapping tree into plain dicts/lists."""
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _field_names(cls: type) -> set[str]:
    model_fields = getattr(cls, "model_fields", None)
    if model_fields is not None:
        return set(model_fields)
    return {f.name for f in dataclasses.fields(cls)}


def _verdict(**overrides: object) -> ClassifierVerdict:
    fields: dict[str, object] = {
        "label": "company",
        "confidence": 0.96,
        "probabilities": {"company": 0.97, "personal": 0.01, "agent_only": 0.01, "unclear": 0.01},
        "personal_probability": 0.02,
        "classifier_id": "jev",
        "classifier_version": "jev-1.13.0",
        "request_id": "req-1",
        "input_tokens": 42,
    }
    fields.update(overrides)
    return ClassifierVerdict(**fields)


# --- the question -------------------------------------------------------------


def test_promotion_question_is_the_canonical_choice_plus_noul() -> None:
    """The stock question v2 (measured best on 227 labelled fleet memories) — its text IS the versioned contract."""
    assert _plain(PROMOTION_QUESTION) == _CANONICAL_QUESTION


def test_promotion_question_has_an_unclear_landing_spot() -> None:
    """OOD inputs need a non-company option (decision 5)."""
    assert set(_plain(PROMOTION_QUESTION)["scope"]["criteria"]) == _LABELS


def test_question_version_is_sha256_of_canonical_json() -> None:
    expected = "sha256:" + hashlib.sha256(canonical_json(_CANONICAL_QUESTION)).hexdigest()

    assert question_version(PROMOTION_QUESTION) == expected


def test_question_version_is_stable_across_calls_and_copies() -> None:
    """Key order and container identity do not move the version (ledger skip works)."""
    reordered = dict(reversed(list(_plain(PROMOTION_QUESTION).items())))

    assert question_version(PROMOTION_QUESTION) == question_version(PROMOTION_QUESTION)
    assert question_version(reordered) == question_version(PROMOTION_QUESTION)


@pytest.mark.parametrize("option", sorted(_LABELS))
def test_question_version_changes_when_an_option_text_changes(option: str) -> None:
    """Any reworded option invalidates every prior verdict in the ledger."""
    edited = _plain(PROMOTION_QUESTION)
    edited["scope"]["criteria"][option]["what"] += " (reworded)"

    assert question_version(edited) != question_version(PROMOTION_QUESTION)


def test_question_version_changes_when_instructions_or_noul_change() -> None:
    choice_edit = _plain(PROMOTION_QUESTION)
    choice_edit["scope"]["instructions"] = "Who owns this?"
    noul_edit = _plain(PROMOTION_QUESTION)
    noul_edit["personal_check"]["instructions"] = "This text is personal."

    canonical = question_version(PROMOTION_QUESTION)

    assert question_version(choice_edit) != canonical
    assert question_version(noul_edit) != canonical


def test_question_version_changes_when_an_option_is_added_or_removed() -> None:
    added = _plain(PROMOTION_QUESTION)
    added["scope"]["criteria"]["vendor"] = {"what": "Vendor chatter."}
    removed = _plain(PROMOTION_QUESTION)
    del removed["scope"]["criteria"]["unclear"]

    canonical = question_version(PROMOTION_QUESTION)

    assert question_version(added) != canonical
    assert question_version(removed) != canonical


# --- input / verdict models ---------------------------------------------------


def test_classifier_input_carries_content_only() -> None:
    """No DID, agent name or path can be sent to the third party (LLM02)."""
    assert _field_names(ClassifierInput) == {"item_kind", "item_id", "content"}


@pytest.mark.parametrize("kind", ["insight", "procedure", "entity"])
def test_classifier_input_accepts_promotable_kinds(kind: str) -> None:
    item = ClassifierInput(item_kind=kind, item_id="x", content="Acme renewal at $42k.")

    assert item.item_kind == kind


@pytest.mark.parametrize("kind", ["episodic", "daily", "note", ""])
def test_classifier_input_rejects_non_promotable_kinds(kind: str) -> None:
    """Episodic events and daily logs can never be shaped into an egress request."""
    with pytest.raises((ValueError, TypeError)):
        ClassifierInput(item_kind=kind, item_id="x", content="raw turn")


def test_classifier_verdict_carries_the_documented_fields() -> None:
    assert _field_names(ClassifierVerdict) == {
        "label",
        "confidence",
        "probabilities",
        "personal_probability",
        "classifier_id",
        "classifier_version",
        "request_id",
        "input_tokens",
    }


def test_classifier_verdict_is_immutable() -> None:
    """A verdict cannot be edited between classify and decide."""
    verdict = _verdict()

    with pytest.raises((ValueError, TypeError, AttributeError, dataclasses.FrozenInstanceError)):
        verdict.label = "personal"  # type: ignore[misc]  # asserting frozen refusal


# --- typed errors -------------------------------------------------------------


def test_classifier_errors_are_distinct_exception_types() -> None:
    """Unavailable (no egress) and call error (outage) drive different sweep statuses."""
    assert issubclass(ClassifierUnavailableError, Exception)
    assert issubclass(ClassifierCallError, Exception)
    assert not issubclass(ClassifierUnavailableError, ClassifierCallError)
    assert not issubclass(ClassifierCallError, ClassifierUnavailableError)


# --- the Protocol, driven by a fake -------------------------------------------


class _FakeClassifier:
    """The only mock in this suite: stands in for the external classifier."""

    classifier_id = "jev"

    def __init__(self, verdict: ClassifierVerdict) -> None:
        self.question_version = question_version(PROMOTION_QUESTION)
        self._verdict = verdict
        self.seen: list[ClassifierInput] = []

    async def ensure_available(self) -> None:
        return None

    async def classify(self, item: ClassifierInput) -> ClassifierVerdict:
        self.seen.append(item)
        return self._verdict


async def test_fake_classifier_satisfies_the_protocol_contract() -> None:
    fake = _FakeClassifier(_verdict())
    classifier: PromotionClassifier = fake
    item = ClassifierInput(item_kind="insight", item_id="acme", content="Acme at $42k.")

    verdict = await classifier.classify(item)

    assert verdict.label == "company"
    assert verdict.classifier_version == "jev-1.13.0"
    assert classifier.question_version.startswith("sha256:")
    assert fake.seen == [item]
