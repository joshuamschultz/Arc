"""arcllm-backed ``PromotionClassifier`` adapter (SPEC-083 COMP-015).

Bridges arcmemory's ``PromotionClassifier`` Protocol (COMP-013) onto arcllm's
provider-agnostic classification seam (``arcllm.classify``, COMP-019) — the
same shape as :mod:`arcmemory.arcllm_seam` (the Embedder/Distiller bridges): a
thin adapter that lives in arcmemory (which already depends on arcllm) so
arcagent never imports arcllm or ``typesafe_sdk`` (REQ-504). This is the only
arcmemory module that names arcllm classification.

Construction resolves nothing: the named drop-in (e.g. ``"jev"``) is looked up
by ``arcllm.classify`` only inside :meth:`ArcllmPromotionClassifier.classify`,
so a classifier instance that never classifies never touches the registry or
a vendor SDK. Only ``item.content`` crosses the seam (LLM02) — no DID, agent
name, item id or kind rides along on the wire.
"""

from __future__ import annotations

import importlib
from typing import cast
from urllib.parse import urlsplit

import arcllm

from arcmemory.promotion.classifier import (
    PROMOTION_QUESTION,
    ClassifierCallError,
    ClassifierInput,
    ClassifierUnavailableError,
    ClassifierVerdict,
    PromotionLabel,
    question_version,
)

_QUESTION_VERSION = question_version(PROMOTION_QUESTION)


def _resolve_host(provider: str) -> str | None:
    """The named drop-in's declared default base URL host, or ``None``.

    Reads the in-tree drop-in module's own ``DEFAULT_BASE_URL`` constant (e.g.
    ``arcllm.classifiers.jev.DEFAULT_BASE_URL``) instead of naming a host
    here, so the egress destination has exactly one source of truth. A
    drop-in that declares no such constant, or one that is not installed (the
    removable-extension case, ADR-038), yields ``None`` — the audit then
    records "cannot name a host" rather than a guess.
    """
    try:
        module = importlib.import_module(f"arcllm.classifiers.{provider}")
    except ImportError:
        return None
    base_url = getattr(module, "DEFAULT_BASE_URL", None)
    if not isinstance(base_url, str) or not base_url:
        return None
    return urlsplit(base_url).hostname


def _build_request(content: str) -> arcllm.ClassificationRequest:
    """``PROMOTION_QUESTION`` translated to arcllm's neutral wire types.

    ``state`` is ``content`` exactly — not stripped, not prefixed.
    """
    scope = PROMOTION_QUESTION["scope"]
    check = PROMOTION_QUESTION["personal_check"]
    return arcllm.ClassificationRequest(
        state=content,
        questions={
            "scope": arcllm.ChoiceSpec(
                instructions=scope["instructions"], criteria=scope["criteria"]
            ),
            "personal_check": arcllm.NoulSpec(instructions=check["instructions"]),
        },
    )


def _to_verdict(classifier_id: str, result: arcllm.ClassificationResult) -> ClassifierVerdict:
    """Map a validated arcllm result field-by-field onto the neutral verdict.

    ``arcllm.classify`` already guarantees every asked question was answered
    with the right answer *kind*, but its ``criteria`` are opaque strings to
    arcllm, so a label or probability key outside the four this seam trusts
    is not its problem to catch — ``ClassifierVerdict``'s ``PromotionLabel``
    typing does, and the caller turns that failure into
    ``ClassifierCallError``.
    """
    scope = result.answers.get("scope")
    check = result.answers.get("personal_check")
    if not isinstance(scope, arcllm.ChoiceResult) or not isinstance(check, arcllm.NoulResult):
        raise TypeError("classifier answered the wrong question kind")
    return ClassifierVerdict(
        label=cast(PromotionLabel, scope.choice),
        confidence=scope.confidence,
        probabilities=cast("dict[PromotionLabel, float]", dict(scope.probabilities)),
        personal_probability=check.noul,
        classifier_id=classifier_id,
        classifier_version=result.model,
        request_id=result.request_id,
        input_tokens=result.input_tokens,
    )


class ArcllmPromotionClassifier:
    """arcmemory ``PromotionClassifier`` (COMP-013) backed by ``arcllm.classify``.

    ``provider`` names an in-tree arcllm classifier drop-in (e.g. ``"jev"``,
    COMP-019); ``model`` is the pinned classifier-model identifier from
    config — never a floating alias. ``timeout`` bounds each ``classify``
    call.
    """

    def __init__(self, provider: str, model: str, timeout: float) -> None:
        self._provider = provider
        self._model = model
        self._timeout = timeout
        self.classifier_id = provider
        self.question_version = _QUESTION_VERSION
        self.host = _resolve_host(provider)

    async def classify(self, item: ClassifierInput) -> ClassifierVerdict:
        """Classify ``item.content`` (only) via arcllm; map errors and result.

        Raises:
            ClassifierUnavailableError: no classifier can be reached — the
                drop-in is absent, its extra is not installed, or no key is
                configured (mirrors ``arcllm.ArcLLMClassifierUnavailableError``).
                Nothing egresses.
            ClassifierCallError: the call failed (outage, timeout) or arcllm
                returned a result that does not answer this seam's contract
                (unknown label, malformed shape). The original arcllm/vendor
                exception is never chained (``from None``), so no transport
                detail or credential rides the error.
        """
        request = _build_request(item.content)
        try:
            result = await arcllm.classify(
                self._provider, self._model, request, timeout=self._timeout
            )
        except arcllm.ArcLLMClassifierUnavailableError as exc:
            raise ClassifierUnavailableError(str(exc)) from None
        except arcllm.ArcLLMError as exc:
            raise ClassifierCallError(str(exc)) from None
        try:
            return _to_verdict(self.classifier_id, result)
        except Exception as exc:  # reason: any malformed/unexpected shape fails closed, typed
            raise ClassifierCallError(
                f"malformed classifier result ({type(exc).__name__})"
            ) from None


__all__ = ["ArcllmPromotionClassifier"]
