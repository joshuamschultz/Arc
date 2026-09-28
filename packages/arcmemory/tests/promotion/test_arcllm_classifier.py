"""T-1201 (SPEC-083 COMP-015) — the arcllm-backed ``PromotionClassifier`` adapter.

RED intent: ``arcmemory.promotion.arcllm_classifier`` does not exist yet, so every
test fails with ``ModuleNotFoundError`` — the feature is absent.

Contract under test (SDD COMP-013 / COMP-015, README decisions 4, 5, 10):

- ``ArcllmPromotionClassifier(provider="jev", model=<pinned>, timeout=<s>)``
  implements ``PromotionClassifier``: ``classifier_id``, ``question_version`` and
  ``async classify(ClassifierInput) -> ClassifierVerdict``.
- It calls ``arcllm.classify(...)`` with ``PROMOTION_QUESTION`` translated to
  ``ChoiceSpec`` (``scope``) + ``NoulSpec`` (``personal_check``).
- The ``state`` sent is ``ClassifierInput.content`` exactly — no DID, agent name,
  item id or kind rides along (LLM02).
- Result → verdict: ``label`` = scope choice, ``confidence`` + ``probabilities``
  from the choice, ``personal_probability`` = the Noul, ``classifier_version`` =
  the model arcllm REPORTS (not the one requested), ``request_id``, ``input_tokens``.
- ``ArcLLMClassifierUnavailableError`` → ``ClassifierUnavailableError``; any other
  arcllm error → ``ClassifierCallError``.

Faked at the boundary only: the drop-in classifier returned by arcllm's registry
(``arcllm.classifiers.load_classifier``). The real ``arcllm.classify`` guard —
timeout, result validation, telemetry — runs on every test. No network.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import arcllm
import arcllm.classifiers
import pytest
from arcllm import (
    ArcLLMClassifierError,
    ArcLLMClassifierUnavailableError,
    ChoiceResult,
    ChoiceSpec,
    ClassificationRequest,
    ClassificationResult,
    ClassifierProvider,
    NoulResult,
    NoulSpec,
)
from arcmemory.promotion.arcllm_classifier import ArcllmPromotionClassifier

from arcmemory.promotion.classifier import (
    PROMOTION_QUESTION,
    ClassifierCallError,
    ClassifierInput,
    ClassifierUnavailableError,
    ClassifierVerdict,
    question_version,
)

_MODEL = "jev-1.13"
_CONTENT = "  Acme renewal closes at $42k/yr; procurement needs a signed PO.\n"
_ITEM = ClassifierInput(item_kind="insight", item_id="acme-renewal-7f3", content=_CONTENT)
_PROBS = {"company": 0.97, "personal": 0.01, "agent_only": 0.01, "unclear": 0.01}


def _result(
    *,
    model: str = _MODEL,
    choice: str = "company",
    confidence: float = 0.97,
    probabilities: dict[str, float] | None = None,
    noul: float | None = 0.03,
    request_id: str | None = "req-123",
    input_tokens: int | None = 17,
) -> ClassificationResult:
    answers: dict[str, Any] = {
        "scope": ChoiceResult(
            choice=choice,
            confidence=confidence,
            probabilities=probabilities if probabilities is not None else _PROBS,
        )
    }
    if noul is not None:
        answers["personal_check"] = NoulResult(noul=noul)
    return ClassificationResult(
        model=model, answers=answers, request_id=request_id, input_tokens=input_tokens
    )


class _FakeDropIn(ClassifierProvider):
    """A drop-in classifier: records each request, answers or raises as scripted."""

    def __init__(
        self,
        model: str,
        *,
        result: ClassificationResult | None = None,
        exc: BaseException | None = None,
        delay: float = 0.0,
    ) -> None:
        self._model = model
        self._result = result if result is not None else _result()
        self._exc = exc
        self._delay = delay
        self.requests: list[ClassificationRequest] = []

    @property
    def model_name(self) -> str:
        return self._model

    async def classify(self, request: ClassificationRequest) -> ClassificationResult:
        self.requests.append(request)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._exc is not None:
            raise self._exc
        return self._result


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Install a fake drop-in behind arcllm's classifier registry.

    ``resolve_classifier`` imports ``load_classifier`` from ``arcllm.classifiers``
    at call time, so replacing it there is the registry boundary. Tests set
    ``registry["drop_in"]`` before classifying; lookups are recorded.
    """
    state: dict[str, Any] = {"drop_in": _FakeDropIn(_MODEL), "lookups": []}

    def _load(name: str, model: str) -> ClassifierProvider:
        state["lookups"].append((name, model))
        drop_in: ClassifierProvider = state["drop_in"]
        return drop_in

    monkeypatch.setattr(arcllm.classifiers, "load_classifier", _load)
    return state


def _classifier(timeout: float = 5.0) -> ArcllmPromotionClassifier:
    return ArcllmPromotionClassifier(provider="jev", model=_MODEL, timeout=timeout)


# -- seam identity -------------------------------------------------------------


def test_classifier_exposes_protocol_identity_without_any_call(registry: dict[str, Any]) -> None:
    clf = _classifier()

    assert clf.classifier_id == "jev"
    assert clf.question_version == question_version(PROMOTION_QUESTION)
    assert registry["lookups"] == []  # construction never resolves or calls a classifier


# -- the request sent ----------------------------------------------------------


async def test_classify_resolves_configured_provider_and_pinned_model(
    registry: dict[str, Any],
) -> None:
    await _classifier().classify(_ITEM)

    assert registry["lookups"] == [("jev", _MODEL)]


async def test_state_sent_is_exactly_the_item_content_and_nothing_else(
    registry: dict[str, Any],
) -> None:
    await _classifier().classify(_ITEM)

    (request,) = registry["drop_in"].requests
    assert request.state == _CONTENT  # byte-for-byte: not stripped, not prefixed
    wire = json.dumps(request.model_dump(mode="json"))
    assert _ITEM.item_id not in wire
    assert "did:" not in wire


async def test_questions_are_promotion_question_as_choice_plus_noul(
    registry: dict[str, Any],
) -> None:
    await _classifier().classify(_ITEM)

    (request,) = registry["drop_in"].requests
    assert set(request.questions) == {"scope", "personal_check"}
    scope = request.questions["scope"]
    check = request.questions["personal_check"]
    assert isinstance(scope, ChoiceSpec)
    assert isinstance(check, NoulSpec)
    expected_scope = PROMOTION_QUESTION["scope"]
    assert scope.instructions == expected_scope["instructions"]
    assert json.loads(json.dumps(scope.criteria, default=dict)) == json.loads(
        json.dumps(expected_scope["criteria"], default=dict)
    )
    assert check.instructions == PROMOTION_QUESTION["personal_check"]["instructions"]


# -- result -> verdict ---------------------------------------------------------


async def test_result_maps_to_verdict_field_by_field(registry: dict[str, Any]) -> None:
    clf = _classifier()

    verdict = await clf.classify(_ITEM)

    assert isinstance(verdict, ClassifierVerdict)
    assert verdict.label == "company"
    assert verdict.confidence == pytest.approx(0.97)
    assert verdict.probabilities == pytest.approx(_PROBS)
    assert verdict.personal_probability == pytest.approx(0.03)
    assert verdict.classifier_id == clf.classifier_id
    assert verdict.classifier_version == _MODEL
    assert verdict.request_id == "req-123"
    assert verdict.input_tokens == 17


async def test_non_company_choice_is_carried_verbatim(registry: dict[str, Any]) -> None:
    probs = {"company": 0.02, "personal": 0.95, "agent_only": 0.02, "unclear": 0.01}
    registry["drop_in"] = _FakeDropIn(
        _MODEL, result=_result(choice="personal", confidence=0.95, probabilities=probs, noul=0.9)
    )

    verdict = await _classifier().classify(_ITEM)

    assert verdict.label == "personal"
    assert verdict.personal_probability == pytest.approx(0.9)


async def test_classifier_version_is_the_reported_model_not_the_requested_one(
    registry: dict[str, Any],
) -> None:
    """A server that silently answers with another model must be visible, so the
    decision rule's pinned-version check can keep the item private."""
    registry["drop_in"] = _FakeDropIn(_MODEL, result=_result(model="jev-1.14"))

    verdict = await _classifier().classify(_ITEM)

    assert verdict.classifier_version == "jev-1.14"


async def test_optional_request_id_and_tokens_pass_through_as_none(
    registry: dict[str, Any],
) -> None:
    registry["drop_in"] = _FakeDropIn(_MODEL, result=_result(request_id=None, input_tokens=None))

    verdict = await _classifier().classify(_ITEM)

    assert verdict.request_id is None
    assert verdict.input_tokens is None


# -- error mapping -------------------------------------------------------------


async def test_unavailable_classifier_maps_to_classifier_unavailable(
    registry: dict[str, Any],
) -> None:
    registry["drop_in"] = _FakeDropIn(
        _MODEL, exc=ArcLLMClassifierUnavailableError(_MODEL, "no API key")
    )

    with pytest.raises(ClassifierUnavailableError):
        await _classifier().classify(_ITEM)


async def test_registry_without_the_drop_in_maps_to_classifier_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stripped-extension path: the registry itself reports unavailable."""

    def _absent(name: str, model: str) -> ClassifierProvider:
        raise ArcLLMClassifierUnavailableError(model, f"no classifier drop-in named '{name}'")

    monkeypatch.setattr(arcllm.classifiers, "load_classifier", _absent)

    with pytest.raises(ClassifierUnavailableError):
        await _classifier().classify(_ITEM)


async def test_call_failure_maps_to_classifier_call_error(registry: dict[str, Any]) -> None:
    registry["drop_in"] = _FakeDropIn(_MODEL, exc=ArcLLMClassifierError("HTTP 529"))

    with pytest.raises(ClassifierCallError):
        await _classifier().classify(_ITEM)


async def test_unexpected_provider_exception_maps_to_classifier_call_error(
    registry: dict[str, Any],
) -> None:
    registry["drop_in"] = _FakeDropIn(_MODEL, exc=RuntimeError("socket reset"))

    with pytest.raises(ClassifierCallError):
        await _classifier().classify(_ITEM)


async def test_configured_timeout_bounds_the_call(registry: dict[str, Any]) -> None:
    registry["drop_in"] = _FakeDropIn(_MODEL, delay=2.0)

    with pytest.raises(ClassifierCallError):
        await asyncio.wait_for(_classifier(timeout=0.05).classify(_ITEM), timeout=1.0)


async def test_missing_personal_check_answer_is_a_call_error(registry: dict[str, Any]) -> None:
    registry["drop_in"] = _FakeDropIn(_MODEL, result=_result(noul=None))

    with pytest.raises(ClassifierCallError):
        await _classifier().classify(_ITEM)


async def test_choice_outside_the_four_labels_is_a_call_error(registry: dict[str, Any]) -> None:
    registry["drop_in"] = _FakeDropIn(_MODEL, result=_result(choice="shareable"))

    with pytest.raises(ClassifierCallError):
        await _classifier().classify(_ITEM)


async def test_probabilities_with_an_unknown_label_are_a_call_error(
    registry: dict[str, Any],
) -> None:
    """arcllm validates the chosen option, not the distribution's keys; a label
    outside the four must not slip into a verdict (or escape as a raw
    validation error the sweep does not expect)."""
    probs = {"company": 0.97, "shareable": 0.03}
    registry["drop_in"] = _FakeDropIn(_MODEL, result=_result(probabilities=probs))

    with pytest.raises(ClassifierCallError):
        await _classifier().classify(_ITEM)


async def test_call_error_text_never_carries_the_classified_content(
    registry: dict[str, Any],
) -> None:
    registry["drop_in"] = _FakeDropIn(_MODEL, exc=RuntimeError(_CONTENT))

    with pytest.raises(ClassifierCallError) as info:
        await _classifier().classify(_ITEM)

    assert "Acme renewal" not in str(info.value)


async def test_unavailable_and_call_errors_stay_distinct(registry: dict[str, Any]) -> None:
    registry["drop_in"] = _FakeDropIn(_MODEL, exc=ArcLLMClassifierError("HTTP 401"))

    with pytest.raises(ClassifierCallError) as info:
        await _classifier().classify(_ITEM)

    assert not isinstance(info.value, ClassifierUnavailableError)
