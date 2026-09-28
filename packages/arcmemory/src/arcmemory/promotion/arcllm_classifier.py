"""arcllm-backed ``PromotionClassifier`` adapter (SPEC-083 COMP-015).

Bridges arcmemory's ``PromotionClassifier`` Protocol (COMP-013) onto arcllm's
provider-agnostic classification seam (``arcllm.classify``, COMP-019) — the
same shape as :mod:`arcmemory.arcllm_seam` (the Embedder/Distiller bridges): a
thin adapter that lives in arcmemory (which already depends on arcllm) so
arcagent never imports arcllm or ``typesafe_sdk`` (REQ-504). This is the only
arcmemory module that names arcllm classification.

Construction resolves nothing: the named drop-in (e.g. ``"jev"``) is looked up
only inside :meth:`ArcllmPromotionClassifier.ensure_available` (the sweep's
network-free preflight) and :meth:`ArcllmPromotionClassifier.classify`, so a
classifier instance that is never used never touches the registry or a vendor
SDK. Only ``item.content`` crosses the seam (LLM02) — no DID, agent
name, item id or kind rides along on the wire.
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Mapping
from typing import Any, cast
from urllib.parse import urlsplit

import arcllm
from arcprompt import PromptSource, StockPromptSource

from arcmemory.promotion.classifier import (
    ClassifierCallError,
    ClassifierInput,
    ClassifierUnavailableError,
    ClassifierVerdict,
    PromotionLabel,
    question_version,
)
from arcmemory.promotion.question import load_promotion_question


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


def _build_request(question: Mapping[str, Any], content: str) -> arcllm.ClassificationRequest:
    """The parsed promotion question translated to arcllm's neutral wire types.

    ``state`` is ``content`` exactly — not stripped, not prefixed.
    """
    scope = question["scope"]
    check = question["personal_check"]
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
    call. ``api_key_env`` / ``vault_path`` are the configured key coordinate,
    forwarded to the drop-in (``None`` keeps the drop-in's default).

    ``prompts`` answers the question prompt (``arcmemory/promotion_classify``):
    the agent's overlay-aware source, or stock when standalone. The question is
    re-read at every :meth:`ensure_available` (once per sweep), so an operator
    edit changes :attr:`question_version` and re-evaluates items on the next
    sweep; within a sweep the question and its version stay fixed.
    """

    def __init__(
        self,
        provider: str,
        model: str,
        timeout: float,
        *,
        api_key_env: str | None = None,
        vault_path: str | None = None,
        prompts: PromptSource | None = None,
    ) -> None:
        self._provider = provider
        self._model = model
        self._timeout = timeout
        self._api_key_env = api_key_env
        self._vault_path = vault_path
        self._prompts: PromptSource = prompts if prompts is not None else StockPromptSource()
        self._question: Mapping[str, Any] | None = None
        self.classifier_id = provider
        self.host = _resolve_host(provider)

    @property
    def question_version(self) -> str:
        """The version of the question this classifier asks (loaded on first read).

        Raises:
            PromotionQuestionInvalidError: the question prompt does not parse.
        """
        return question_version(self._current_question())

    def _current_question(self) -> Mapping[str, Any]:
        if self._question is None:
            self._question = load_promotion_question(self._prompts)
        return self._question

    async def ensure_available(self) -> None:
        """Network-free preflight: the question parses, the drop-in and its key resolve.

        Re-reads the question prompt, then runs the drop-in's ``check_available``
        (the same lookup and key path ``classify`` takes) — both in a worker
        thread, since prompt resolution reads files and key resolution may read
        a vault. No client is built and nothing is sent.

        Raises:
            PromotionQuestionInvalidError: the question prompt cannot be
                resolved or does not parse (a ``ClassifierUnavailableError``).
            ClassifierUnavailableError: the classifier cannot serve. Any arcllm
                error here means the same thing — no request was attempted.
        """
        self._question = await asyncio.to_thread(load_promotion_question, self._prompts)
        try:
            await asyncio.to_thread(self._check_available)
        except arcllm.ArcLLMError as exc:
            raise ClassifierUnavailableError(str(exc)) from None

    def _check_available(self) -> None:
        drop_in = arcllm.resolve_classifier(
            self._provider,
            self._model,
            api_key_env=self._api_key_env,
            vault_path=self._vault_path,
        )
        drop_in.check_available()

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
        question = self._question
        if question is None:
            question = await asyncio.to_thread(self._current_question)
        request = _build_request(question, item.content)
        try:
            result = await arcllm.classify(
                self._provider,
                self._model,
                request,
                timeout=self._timeout,
                api_key_env=self._api_key_env,
                vault_path=self._vault_path,
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
