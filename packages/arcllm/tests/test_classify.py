"""Classification seam — ``arcllm.classify`` (SPEC-083 T-1199, COMP-019).

The seam mirrors ``arcllm.embeddings``: an ABC (``ClassifierProvider``), a
name registry (``resolve_classifier``) with no dynamic import path from config,
and one public ``classify()`` that adds the timeout, typed errors, telemetry
and the SPEC-038 budget guard on top of any provider.

These tests drive the seam with an in-memory fake provider (no network, no
``typesafe_sdk``). The Jev plugin itself is covered in
``test_jev_classifier.py``.

Assumed interface (the contract these tests pin):

    arcllm.classify.ChoiceSpec(instructions: str, criteria: Mapping[str, str | Mapping])
    arcllm.classify.NoulSpec(instructions: str, criteria: Mapping | None = None)
    arcllm.classify.ClassificationRequest(state: str, questions: Mapping[str, ChoiceSpec | NoulSpec])
    arcllm.classify.ChoiceResult(choice: str, confidence: float, probabilities: Mapping[str, float])
    arcllm.classify.NoulResult(noul: float)
    arcllm.classify.ClassificationResult(model: str, answers: Mapping[str, ChoiceResult | NoulResult],
                                         request_id: str | None, input_tokens: int | None)
    class arcllm.classify.ClassifierProvider(ABC):
        model_name: str  (abstract property)
        async def classify(self, request: ClassificationRequest) -> ClassificationResult  (abstract)
    arcllm.classify.resolve_classifier(name: str, model: str) -> ClassifierProvider
    async arcllm.classify.classify(provider: ClassifierProvider | str, model: str,
                                   request: ClassificationRequest, *, timeout: float = ...,
                                   telemetry: dict | None = None,
                                   on_event: Callable[[SpoolRecord], None] | None = None)
                                   -> ClassificationResult
    arcllm.ArcLLMClassifierError, arcllm.ArcLLMClassifierUnavailableError  (both ArcLLMError;
        distinct — Unavailable is NOT a subclass of ArcLLMClassifierError).
        ArcLLMClassifierUnavailableError(model: str, reason: str) mirrors
        ArcLLMEmbeddingUnavailableError.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from arcllm.classify import (
    ChoiceResult,
    ChoiceSpec,
    ClassificationRequest,
    ClassificationResult,
    ClassifierProvider,
    NoulResult,
    NoulSpec,
    classify,
    resolve_classifier,
)

import arcllm
from arcllm import ArcLLMClassifierError, ArcLLMClassifierUnavailableError, ArcLLMError
from arcllm.exceptions import ArcLLMBudgetError, ArcLLMConfigError
from arcllm.modules.telemetry_budget import clear_budgets

MODEL = "jev-1.13"
STATE_MARKER = "SENTINEL-STATE-7731"
STATE = f"Acme renewal closes at $42k/yr, net-60. {STATE_MARKER}"

_SCOPE_OPTIONS = ("company", "personal", "agent_only", "unclear")


def _request(state: str = STATE) -> ClassificationRequest:
    return ClassificationRequest(
        state=state,
        questions={
            "scope": ChoiceSpec(
                instructions="Who is this remembered fact or method useful to?",
                criteria={
                    "company": "The business: operations, deals, market, reusable processes.",
                    "personal": "The operator's personal life or personal side projects.",
                    "agent_only": "Housekeeping only this one assistant needs.",
                    "unclear": "None of these, or not enough information to tell.",
                },
            ),
            "personal_check": NoulSpec(
                instructions="This text is about the operator's personal life or personal projects."
            ),
        },
    )


def _result(
    *,
    choice: str = "company",
    confidence: float = 0.98,
    noul: float = 0.03,
    input_tokens: int | None = 42,
    request_id: str | None = "req_abc123",
) -> ClassificationResult:
    probabilities = {option: 0.0 for option in _SCOPE_OPTIONS}
    probabilities[choice] = 1.0 - 0.015 * 3
    for option in _SCOPE_OPTIONS:
        if option != choice:
            probabilities[option] = 0.015
    return ClassificationResult(
        model=MODEL,
        answers={
            "scope": ChoiceResult(
                choice=choice, confidence=confidence, probabilities=probabilities
            ),
            "personal_check": NoulResult(noul=noul),
        },
        request_id=request_id,
        input_tokens=input_tokens,
    )


class _FakeClassifier(ClassifierProvider):
    """In-memory provider: records every request, returns a canned result."""

    def __init__(
        self,
        result: ClassificationResult | None = None,
        *,
        error: BaseException | None = None,
        hang: bool = False,
    ) -> None:
        self._result = result if result is not None else _result()
        self._error = error
        self._hang = hang
        self.requests: list[ClassificationRequest] = []

    @property
    def model_name(self) -> str:
        return MODEL

    async def classify(self, request: ClassificationRequest) -> ClassificationResult:
        self.requests.append(request)
        if self._hang:
            await asyncio.sleep(3600)
        if self._error is not None:
            raise self._error
        return self._result


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # Spool writes land in a per-test dir so the telemetry file can be inspected.
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(tmp_path / "arcstore"))
    clear_budgets()
    yield
    clear_budgets()


def _spooled_bytes(tmp_path: Path) -> bytes:
    root = tmp_path / "arcstore"
    if not root.exists():
        return b""
    return b"".join(p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file())


# ---------------------------------------------------------------------------
# Contract: exports + error taxonomy
# ---------------------------------------------------------------------------


def test_arcllm_root_exports_classification_seam() -> None:
    for name in (
        "classify",
        "resolve_classifier",
        "ClassifierProvider",
        "ClassificationRequest",
        "ClassificationResult",
        "ChoiceSpec",
        "NoulSpec",
        "ArcLLMClassifierError",
        "ArcLLMClassifierUnavailableError",
    ):
        assert hasattr(arcllm, name), f"arcllm does not export {name}"


def test_classifier_error_types_are_distinct_arcllm_errors() -> None:
    """The sweep maps unavailable -> classifier_unavailable and every other
    failure -> classifier_error; a subclass relation would let one swallow the other."""
    assert issubclass(ArcLLMClassifierError, ArcLLMError)
    assert issubclass(ArcLLMClassifierUnavailableError, ArcLLMError)
    assert not issubclass(ArcLLMClassifierUnavailableError, ArcLLMClassifierError)
    assert not issubclass(ArcLLMClassifierError, ArcLLMClassifierUnavailableError)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_classify_returns_provider_result_for_exact_request() -> None:
    fake = _FakeClassifier()
    request = _request()

    result = await classify(
        provider=fake, model=MODEL, request=request, telemetry={"arcstore_enabled": False}
    )

    assert fake.requests == [request]
    assert result.model == MODEL
    scope = result.answers["scope"]
    assert isinstance(scope, ChoiceResult)
    assert scope.choice == "company"
    assert scope.confidence == pytest.approx(0.98)
    assert set(scope.probabilities) == set(_SCOPE_OPTIONS)
    check = result.answers["personal_check"]
    assert isinstance(check, NoulResult)
    assert check.noul == pytest.approx(0.03)
    assert result.request_id == "req_abc123"
    assert result.input_tokens == 42


# ---------------------------------------------------------------------------
# Error paths — typed, fail closed
# ---------------------------------------------------------------------------


async def test_classify_propagates_unavailable_unchanged() -> None:
    fake = _FakeClassifier(
        error=ArcLLMClassifierUnavailableError(MODEL, "jev extra not installed")
    )

    with pytest.raises(ArcLLMClassifierUnavailableError):
        await classify(
            provider=fake, model=MODEL, request=_request(), telemetry={"arcstore_enabled": False}
        )


async def test_classify_wraps_unexpected_provider_exception_as_classifier_error() -> None:
    fake = _FakeClassifier(error=RuntimeError("socket reset"))

    with pytest.raises(ArcLLMClassifierError):
        await classify(
            provider=fake, model=MODEL, request=_request(), telemetry={"arcstore_enabled": False}
        )


async def test_classify_timeout_bounds_a_hanging_provider() -> None:
    fake = _FakeClassifier(hang=True)
    started = time.monotonic()

    with pytest.raises(ArcLLMClassifierError):
        await classify(
            provider=fake,
            model=MODEL,
            request=_request(),
            timeout=0.05,
            telemetry={"arcstore_enabled": False},
        )

    assert time.monotonic() - started < 2.0


@pytest.mark.parametrize(
    "malformed",
    [
        pytest.param(
            ClassificationResult(
                model=MODEL,
                answers={"personal_check": NoulResult(noul=0.03)},
                request_id="r",
                input_tokens=1,
            ),
            id="missing-requested-question",
        ),
        pytest.param(
            ClassificationResult(
                model=MODEL,
                answers={"scope": NoulResult(noul=0.9), "personal_check": NoulResult(noul=0.03)},
                request_id="r",
                input_tokens=1,
            ),
            id="choice-question-answered-as-noul",
        ),
        pytest.param(
            ClassificationResult(
                model=MODEL,
                answers={
                    "scope": ChoiceResult(
                        choice="banana",
                        confidence=0.99,
                        probabilities={"banana": 1.0},
                    ),
                    "personal_check": NoulResult(noul=0.03),
                },
                request_id="r",
                input_tokens=1,
            ),
            id="choice-not-among-options",
        ),
    ],
)
async def test_classify_rejects_result_that_does_not_answer_the_request(
    malformed: ClassificationResult,
) -> None:
    """A provider result that does not answer the questions asked is malformed
    and must fail closed — it can never be read as a verdict."""
    fake = _FakeClassifier(malformed)

    with pytest.raises(ArcLLMClassifierError):
        await classify(
            provider=fake, model=MODEL, request=_request(), telemetry={"arcstore_enabled": False}
        )


# ---------------------------------------------------------------------------
# Telemetry — recorded, but never the state text
# ---------------------------------------------------------------------------


async def test_classify_emits_llm_call_telemetry_with_model_and_tokens() -> None:
    from arcstore.records import SpoolRecord

    captured: list[SpoolRecord] = []
    await classify(
        provider=_FakeClassifier(),
        model=MODEL,
        request=_request(),
        telemetry={"arcstore_enabled": False},
        on_event=captured.append,
    )

    assert len(captured) == 1
    record = captured[0]
    assert record.kind == "llm_call"
    assert record.model == MODEL
    assert record.prompt_tokens == 42
    assert "req_abc123" in record.model_dump_json(), "request_id missing from telemetry"


async def test_classify_telemetry_never_contains_state_even_with_raw_bodies_on(
    tmp_path: Path,
) -> None:
    """Memory text sent for classification is sensitive; the trace/spool must
    carry shape only — even when ``store_raw_bodies`` is on (the embed default)."""
    from arcstore.records import SpoolRecord

    captured: list[SpoolRecord] = []
    await classify(
        provider=_FakeClassifier(),
        model=MODEL,
        request=_request(),
        telemetry={"store_raw_bodies": True, "agent_did": "did:arc:local:executor/t"},
        on_event=captured.append,
    )

    assert captured, "no telemetry record emitted"
    assert STATE_MARKER not in captured[0].model_dump_json()
    spooled = _spooled_bytes(tmp_path)
    assert spooled, "classify did not spool its llm_call record"
    assert STATE_MARKER.encode() not in spooled


async def test_classify_failure_does_not_leak_state_into_exception_text() -> None:
    fake = _FakeClassifier(error=RuntimeError("upstream exploded"))

    with pytest.raises(ArcLLMClassifierError) as info:
        await classify(
            provider=fake, model=MODEL, request=_request(), telemetry={"arcstore_enabled": False}
        )

    assert STATE_MARKER not in str(info.value)
    assert STATE_MARKER not in repr(info.value)


# ---------------------------------------------------------------------------
# Budget — same SPEC-038 guard as embed()
# ---------------------------------------------------------------------------


async def test_classify_over_budget_raises_standard_breach_before_calling_provider() -> None:
    fake = _FakeClassifier(_result(input_tokens=3))
    tel = {
        "budget_scope": "agent:classify-over-budget",
        "cost_input_per_1m": 1_000_000.0,  # $1 per token
        "monthly_limit_usd": 2.0,
        "enforcement": "block",
        "arcstore_enabled": False,
    }
    await classify(provider=fake, model=MODEL, request=_request(), telemetry=tel)  # $3 spent

    with pytest.raises(ArcLLMBudgetError):
        await classify(provider=fake, model=MODEL, request=_request(), telemetry=tel)

    assert len(fake.requests) == 1, "the over-budget call still reached the provider"


# ---------------------------------------------------------------------------
# Registry — in-tree names only, no dynamic import from config
# ---------------------------------------------------------------------------


def test_resolve_classifier_jev_is_lazy_about_the_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "typesafe_sdk", None)  # SDK physically absent

    provider = resolve_classifier("jev", MODEL)

    assert isinstance(provider, ClassifierProvider)
    assert provider.model_name == MODEL


@pytest.mark.parametrize(
    "name",
    ["no-such-classifier", "os:system", "arcllm.classifiers.jev:JevClassifier", ""],
)
def test_resolve_classifier_rejects_unknown_or_dotted_names(name: str) -> None:
    with pytest.raises(ArcLLMConfigError):
        resolve_classifier(name, MODEL)


async def test_classify_by_name_with_sdk_absent_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "typesafe_sdk", None)
    monkeypatch.setenv("TYPESAFE_API_KEY", "tsk_present_but_sdk_missing")

    with pytest.raises(ArcLLMClassifierUnavailableError):
        await classify(
            provider="jev", model=MODEL, request=_request(), telemetry={"arcstore_enabled": False}
        )
