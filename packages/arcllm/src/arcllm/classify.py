"""Classification seam — arcllm owns classifier *inference* and nothing else.

SPEC-083 COMP-019. A caller asks named questions about one piece of text
(``ClassificationRequest``) and gets typed answers back
(``ClassificationResult``). Concrete classifiers are removable drop-ins under
``arcllm/classifiers/`` resolved by name (``resolve_classifier``); the core
here never names a vendor.

``classify`` wraps any provider with the same guard ``embed()`` uses: a hard
timeout, the SPEC-038 budget pre-check + spend accounting, an OTel span, and
one ``llm_call`` telemetry record. The classified text is sensitive (it is
agent memory), so telemetry, logs and exception text carry its shape only —
never the text itself, regardless of ``store_raw_bodies``.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field

from arcllm.exceptions import ArcLLMError
from arcllm.modules.call_budget import budget_pre_check, count_tokens, emit_telemetry, parse_budget
from arcllm.modules.telemetry_cost import calculate_cost
from arcllm.types import Usage

if TYPE_CHECKING:  # keep arcstore off the module-import hot path
    from arcstore.records import SpoolRecord

logger = logging.getLogger(__name__)

DEFAULT_CLASSIFY_TIMEOUT_SECONDS = 10.0
_OPERATION = "classify"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ArcLLMClassifierUnavailableError(ArcLLMError):
    """No classifier can serve: the drop-in is absent, its extra is not
    installed, or no API key is configured. Mirrors
    ``ArcLLMEmbeddingUnavailableError`` — a clean typed signal the caller
    degrades on, never an ImportError. Deliberately NOT a subclass of
    ``ArcLLMClassifierError`` so neither can swallow the other."""

    def __init__(self, model: str, reason: str) -> None:
        self.model = model
        self.reason = reason
        super().__init__(f"No classifier available for model '{model}': {reason}")


class ArcLLMClassifierError(ArcLLMError):
    """A classifier call failed or returned an answer that does not answer the
    request (HTTP error, timeout, transport failure, malformed result).
    The message never carries the classified text or a credential."""


# ---------------------------------------------------------------------------
# Contract types
# ---------------------------------------------------------------------------

_FROZEN = ConfigDict(frozen=True, allow_inf_nan=False)


class ChoiceSpec(BaseModel):
    """Pick one option. ``criteria`` maps each option to its description
    (a string, or an object such as ``{"what": ..., "not_for": ...}``)."""

    model_config = _FROZEN

    instructions: str
    criteria: Mapping[str, str | Mapping[str, Any]]


class NoulSpec(BaseModel):
    """Probability that a statement about the text is true."""

    model_config = _FROZEN

    instructions: str
    criteria: Mapping[str, Any] | None = None


class ClassificationRequest(BaseModel):
    """One text (``state``) and the named questions to ask about it."""

    model_config = _FROZEN

    state: str
    questions: Mapping[str, ChoiceSpec | NoulSpec]


class ChoiceResult(BaseModel):
    """The chosen option, its confidence, and the full distribution."""

    model_config = _FROZEN

    choice: str
    confidence: float = Field(ge=0.0, le=1.0)
    probabilities: Mapping[str, float]


class NoulResult(BaseModel):
    """Probability (0..1) that the Noul statement is true."""

    model_config = _FROZEN

    noul: float = Field(ge=0.0, le=1.0)


class ClassificationResult(BaseModel):
    """Answers keyed by question name. ``model`` is the version that actually
    answered (it may differ from the one requested)."""

    model_config = _FROZEN

    model: str
    answers: Mapping[str, ChoiceResult | NoulResult]
    request_id: str | None
    input_tokens: int | None


class ClassifierProvider(ABC):
    """One classifier backend. Callers depend on this, never on a concrete
    drop-in (mirrors ``EmbeddingProvider``)."""

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Pinned classifier-model identifier this backend targets."""

    @abstractmethod
    async def classify(self, request: ClassificationRequest) -> ClassificationResult:
        """Answer ``request``. Raises ``ArcLLMClassifierUnavailableError`` when
        this backend cannot serve, ``ArcLLMClassifierError`` on call failure."""

    def check_available(self) -> None:  # noqa: B027  # reason: optional hook; no-op default
        """Network-free preflight: raise ``ArcLLMClassifierUnavailableError``
        when this backend cannot serve (SDK or key missing). Sends nothing and
        builds no client, so a caller can settle availability before it audits
        an egress. The default has nothing local to check."""


def resolve_classifier(
    name: str,
    model: str,
    *,
    api_key_env: str | None = None,
    vault_path: str | None = None,
) -> ClassifierProvider:
    """Construct the in-tree drop-in classifier registered as ``name``.

    ``api_key_env`` / ``vault_path`` are the operator's key coordinate (never the
    key); ``None`` leaves the drop-in's own default in place.

    Raises:
        ArcLLMConfigError: ``name`` is malformed (dotted, ``module:Class``, empty).
        ArcLLMClassifierUnavailableError: no drop-in named ``name`` is installed.
    """
    from arcllm.classifiers import load_classifier  # drop-ins import this module

    options = {
        key: value
        for key, value in (("api_key_env", api_key_env), ("vault_path", vault_path))
        if value is not None
    }
    return load_classifier(name, model, **options)


# ---------------------------------------------------------------------------
# Result validation — a result that does not answer the request fails closed
# ---------------------------------------------------------------------------


def _finite_unit(value: float) -> bool:
    return math.isfinite(value) and 0.0 <= value <= 1.0


def _choice_answer_problem(spec: ChoiceSpec, answer: ChoiceResult | NoulResult) -> str | None:
    if not isinstance(answer, ChoiceResult):
        return "choice question answered with a non-choice answer"
    if answer.choice not in spec.criteria:
        return "choice is not among the offered options"
    if not all(_finite_unit(p) for p in answer.probabilities.values()):
        return "probabilities are not finite values in [0, 1]"
    return None


def _answer_problem(
    spec: ChoiceSpec | NoulSpec, answer: ChoiceResult | NoulResult | None
) -> str | None:
    if answer is None:
        return "question not answered"
    if isinstance(spec, ChoiceSpec):
        return _choice_answer_problem(spec, answer)
    if not isinstance(answer, NoulResult):
        return "noul question answered with a non-noul answer"
    return None


def _validate_result(request: ClassificationRequest, result: ClassificationResult) -> None:
    """Raise ``ArcLLMClassifierError`` unless ``result`` answers exactly the
    questions asked, each with the right answer kind. Messages name the
    question key only — never the state."""
    unexpected = set(result.answers) - set(request.questions)
    if unexpected:
        raise ArcLLMClassifierError(f"classifier answered unasked questions: {sorted(unexpected)}")
    for key, spec in request.questions.items():
        problem = _answer_problem(spec, result.answers.get(key))
        if problem is not None:
            raise ArcLLMClassifierError(f"malformed classifier result for '{key}': {problem}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def _call_provider(
    provider: ClassifierProvider, request: ClassificationRequest, timeout: float
) -> ClassificationResult:
    """Run the provider under ``timeout`` and map every failure to a typed error.

    The typed error is raised OUTSIDE the ``except`` block so the original
    exception is neither the ``__cause__`` nor the ``__context__`` — a vendor
    exception could carry request text or a credential.
    """
    failure: str
    try:
        return await asyncio.wait_for(provider.classify(request), timeout=timeout)
    except (ArcLLMClassifierError, ArcLLMClassifierUnavailableError):
        raise
    except TimeoutError:
        failure = f"timed out after {timeout}s"
    except Exception as exc:  # reason: any provider failure fails closed as a typed error
        failure = f"failed ({type(exc).__name__})"
    logger.warning("arcllm.classify %s: provider %s", failure, type(provider).__name__)
    raise ArcLLMClassifierError(f"classifier call {failure}")


def _request_shape(request: ClassificationRequest) -> dict[str, Any]:
    """Trace descriptor — shape only. The state is memory text and is never
    recorded, even when ``store_raw_bodies`` is on."""
    return {"questions": sorted(request.questions), "state_chars": len(request.state)}


def _response_shape(result: ClassificationResult) -> dict[str, Any]:
    return {
        "operation": _OPERATION,
        "request_id": result.request_id,
        "answered": sorted(result.answers),
    }


async def classify(
    provider: ClassifierProvider | str,
    model: str,
    request: ClassificationRequest,
    *,
    timeout: float = DEFAULT_CLASSIFY_TIMEOUT_SECONDS,
    telemetry: dict[str, Any] | None = None,
    on_event: Callable[[SpoolRecord], None] | None = None,
    api_key_env: str | None = None,
    vault_path: str | None = None,
) -> ClassificationResult:
    """Classify ``request`` — bounded, budget-routed and telemetered.

    Args:
        provider: A ``ClassifierProvider`` (dependency injection) or the name
            of an in-tree drop-in (e.g. ``"jev"``) resolved with ``model``.
        model: Pinned classifier-model identifier.
        request: The text and the named questions.
        timeout: Hard bound on the whole provider call, in seconds.
        telemetry: SPEC-038 budget + telemetry keys, identical to ``embed()``
            (``budget_scope``, ``monthly_limit_usd``, ``daily_limit_usd``,
            ``per_call_max_usd``, ``cost_input_per_1m``, ``enforcement``,
            ``agent_did``, ``agent_label``, ``arcstore_enabled``).
        on_event: Optional callback fired with the ``SpoolRecord`` telemetry.
        api_key_env: Env var holding the key for a NAMED drop-in (default: the
            drop-in's own). Ignored when ``provider`` is an instance.
        vault_path: Vault path tried before ``api_key_env`` for a named drop-in.

    Raises:
        ArcLLMClassifierUnavailableError: no classifier can serve.
        ArcLLMClassifierError: the call failed or the result is malformed.
        ArcLLMBudgetError: the call would exceed a configured budget limit.
        ArcLLMConfigError: ``provider`` names a malformed classifier.
    """
    tel = telemetry or {}
    if isinstance(provider, str):
        provider_label = provider
        classifier = resolve_classifier(
            provider, model, api_key_env=api_key_env, vault_path=vault_path
        )
    else:
        provider_label = "custom"
        classifier = provider
    cost_input_per_1m = tel.get("cost_input_per_1m", 0.0)
    estimated_tokens = count_tokens([request.state])

    plan = parse_budget(tel)
    if plan is not None:
        budget_pre_check(plan, estimated_tokens, cost_input_per_1m, call_kind="classify")

    tracer = trace.get_tracer("arcllm")
    with tracer.start_as_current_span("arcllm.classify") as span:
        span.set_attribute("arcllm.classify.model", model)
        span.set_attribute("arcllm.classify.questions", len(request.questions))
        t0 = time.monotonic()
        result = await _call_provider(classifier, request, timeout)
        latency_ms = round((time.monotonic() - t0) * 1000, 1)
        _validate_result(request, result)

        input_tokens = result.input_tokens if result.input_tokens is not None else estimated_tokens
        usage = Usage(input_tokens=input_tokens, output_tokens=0, total_tokens=input_tokens)
        cost = calculate_cost(usage, input_per_1m=cost_input_per_1m, output_per_1m=0.0)
        if plan is not None:
            plan.accumulator.deduct(max(0.0, cost))
        span.set_attribute("arcllm.classify.cost_usd", cost)

    emit_telemetry(
        tel,
        on_event,
        provider_label=provider_label,
        model=result.model,
        usage=usage,
        cost=cost,
        latency_ms=latency_ms,
        operation=_OPERATION,
        request_body=_request_shape(request),
        response_body=_response_shape(result),
    )
    return result
