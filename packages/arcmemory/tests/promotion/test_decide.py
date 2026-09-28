"""T-1193 (SPEC-083 COMP-017) — the promote / keep_private decision table.

RED intent: ``arcmemory.promotion.decide`` (and the ``classifier`` models it
consumes) do not exist yet, so the import fails with ``ModuleNotFoundError``.

Contract under test (SDD COMP-017, README decisions 5 and 6):

``decide(verdict, cfg) -> Literal["promote", "keep_private"]`` — pure. Promote iff
ALL hold, otherwise keep_private:

- ``label == "company"``;
- ``confidence >= cfg.confidence_threshold`` (default 0.95) and finite;
- probabilities well-formed: exactly the four labels, all finite, sum 1.0 +/- 0.01,
  ``probabilities["company"]`` is the max;
- ``personal_probability is None`` or ``<= cfg.max_personal_probability`` (0.10);
- ``classifier_version == cfg.classifier_model`` (pinned ``jev-1.13``).

A forged/malformed verdict that bypasses model validation (``model_construct``)
must still land on keep_private: the rule is the last gate before egress-derived
data crosses DID isolation.
"""

from __future__ import annotations

import math

import pytest
from arcmemory.promotion.classifier import ClassifierVerdict
from arcmemory.promotion.decide import decide

from arcmemory.promotion.config import PromotionConfig

_GOOD_PROBS = {"company": 0.97, "personal": 0.01, "agent_only": 0.01, "unclear": 0.01}


def _verdict(**overrides: object) -> ClassifierVerdict:
    """Build a verdict; bypass model validation for deliberately malformed ones.

    If the verdict model itself rejects a malformed value, that is also fail
    closed — but ``decide`` must not rely on it, so the malformed verdict is
    forced through ``model_construct`` when the model is Pydantic.
    """
    fields: dict[str, object] = {
        "label": "company",
        "confidence": 0.96,
        "probabilities": dict(_GOOD_PROBS),
        "personal_probability": 0.02,
        "classifier_id": "jev",
        "classifier_version": "jev-1.13",
        "request_id": "req-1",
        "input_tokens": 42,
    }
    fields.update(overrides)
    try:
        return ClassifierVerdict(**fields)
    except (ValueError, TypeError):
        construct = getattr(ClassifierVerdict, "model_construct", None)
        if construct is None:
            raise
        return construct(**fields)


def _probs(label: str, top: float) -> dict[str, float]:
    rest = (1.0 - top) / 3
    return {name: (top if name == label else rest) for name in _GOOD_PROBS}


# --- the table from PLAN T-1193 -------------------------------------------------


def test_company_at_096_promotes() -> None:
    assert decide(_verdict(), PromotionConfig()) == "promote"


def test_company_at_094_keeps_private() -> None:
    """Below the 0.95 default — Jev's mid band is overconfident."""
    assert decide(_verdict(confidence=0.94), PromotionConfig()) == "keep_private"


def test_unclear_at_099_keeps_private() -> None:
    """OOD inputs land on unclear with 0.99+; that is never a promote."""
    verdict = _verdict(label="unclear", confidence=0.99, probabilities=_probs("unclear", 0.9925))

    assert decide(verdict, PromotionConfig()) == "keep_private"


@pytest.mark.parametrize("label", ["personal", "agent_only"])
def test_non_company_labels_at_099_keep_private(label: str) -> None:
    verdict = _verdict(label=label, confidence=0.99, probabilities=_probs(label, 0.9925))

    assert decide(verdict, PromotionConfig()) == "keep_private"


def test_company_at_099_with_personal_probability_020_keeps_private() -> None:
    """The Noul cross-check vetoes a confident company answer (REQ-490)."""
    verdict = _verdict(
        confidence=0.99, probabilities=_probs("company", 0.9925), personal_probability=0.2
    )

    assert decide(verdict, PromotionConfig()) == "keep_private"


def test_company_with_missing_label_in_probabilities_keeps_private() -> None:
    probs = {"company": 0.98, "personal": 0.01, "agent_only": 0.01}

    assert decide(_verdict(probabilities=probs), PromotionConfig()) == "keep_private"


def test_company_with_nan_probability_keeps_private() -> None:
    probs = {**_GOOD_PROBS, "unclear": math.nan}

    assert decide(_verdict(probabilities=probs), PromotionConfig()) == "keep_private"


def test_company_with_infinite_probability_keeps_private() -> None:
    probs = {**_GOOD_PROBS, "personal": math.inf}

    assert decide(_verdict(probabilities=probs), PromotionConfig()) == "keep_private"


@pytest.mark.parametrize("total_top", [0.90, 1.05])
def test_company_with_probabilities_not_summing_to_one_keeps_private(total_top: float) -> None:
    """Sum 0.93 or 1.08 is outside 1.0 +/- 0.01."""
    probs = {"company": total_top, "personal": 0.01, "agent_only": 0.01, "unclear": 0.01}

    assert decide(_verdict(probabilities=probs), PromotionConfig()) == "keep_private"


def test_company_with_unknown_extra_label_keeps_private() -> None:
    probs = {
        "company": 0.96,
        "personal": 0.01,
        "agent_only": 0.01,
        "unclear": 0.01,
        "vendor": 0.01,
    }

    assert decide(_verdict(probabilities=probs), PromotionConfig()) == "keep_private"


def test_unknown_verdict_label_keeps_private() -> None:
    """A label outside the four options (e.g. a renamed option) never promotes."""
    assert decide(_verdict(label="Company"), PromotionConfig()) == "keep_private"
    assert decide(_verdict(label="shared"), PromotionConfig()) == "keep_private"


def test_wrong_classifier_version_keeps_private() -> None:
    """A verdict from a model other than the pinned one is not trusted."""
    assert decide(_verdict(classifier_version="jev-1.12"), PromotionConfig()) == "keep_private"
    assert decide(_verdict(classifier_version="jev-latest"), PromotionConfig()) == "keep_private"


# --- boundaries and cfg sensitivity ----------------------------------------------


def test_confidence_exactly_at_threshold_promotes() -> None:
    assert decide(_verdict(confidence=0.95), PromotionConfig()) == "promote"


def test_operator_threshold_is_honoured() -> None:
    """With threshold 0.97, the 0.96 that promoted by default now stays private."""
    cfg = PromotionConfig(confidence_threshold=0.97)

    assert decide(_verdict(confidence=0.96), cfg) == "keep_private"
    assert decide(_verdict(confidence=0.97), cfg) == "promote"


def test_personal_probability_exactly_at_cap_promotes() -> None:
    assert decide(_verdict(personal_probability=0.10), PromotionConfig()) == "promote"


def test_personal_probability_just_over_cap_keeps_private() -> None:
    assert decide(_verdict(personal_probability=0.1001), PromotionConfig()) == "keep_private"


def test_personal_probability_absent_promotes() -> None:
    """No Noul answer (cross-check not configured) does not by itself block."""
    assert decide(_verdict(personal_probability=None), PromotionConfig()) == "promote"


def test_personal_probability_nan_keeps_private() -> None:
    assert decide(_verdict(personal_probability=math.nan), PromotionConfig()) == "keep_private"


def test_pinned_model_from_cfg_is_the_version_that_counts() -> None:
    cfg = PromotionConfig(classifier_model="jev-1.14")

    assert decide(_verdict(classifier_version="jev-1.14"), cfg) == "promote"
    assert decide(_verdict(classifier_version="jev-1.13"), cfg) == "keep_private"


@pytest.mark.parametrize("confidence", [math.nan, math.inf, 1.5, -0.1])
def test_malformed_confidence_keeps_private(confidence: float) -> None:
    assert decide(_verdict(confidence=confidence), PromotionConfig()) == "keep_private"


def test_company_label_but_company_not_the_max_probability_keeps_private() -> None:
    """Label and distribution disagree — malformed, never promote."""
    probs = {"company": 0.30, "personal": 0.60, "agent_only": 0.05, "unclear": 0.05}

    assert decide(_verdict(probabilities=probs), PromotionConfig()) == "keep_private"


def test_sum_within_tolerance_promotes() -> None:
    """0.995 is within 1.0 +/- 0.01 (float rounding from the wire)."""
    probs = {"company": 0.965, "personal": 0.01, "agent_only": 0.01, "unclear": 0.01}

    assert decide(_verdict(probabilities=probs), PromotionConfig()) == "promote"


def test_decide_is_pure_and_repeatable() -> None:
    verdict = _verdict()
    cfg = PromotionConfig()

    assert [decide(verdict, cfg) for _ in range(3)] == ["promote"] * 3
    assert verdict.probabilities == _GOOD_PROBS
