"""Jev classifier drop-in — TypeSafe ``POST /v1/systemone`` (SPEC-083 COMP-019).

This folder is the ONE scoped exception to arcllm's httpx-only, no-vendor-SDK
rule (ADR-038): it uses ``typesafe-sdk`` behind the optional ``arcllm[jev]``
extra, and the SDK is imported nowhere else in arcllm. The folder is
removable — delete it (or skip the extra) and ``resolve_classifier("jev")`` /
the call path report ``ArcLLMClassifierUnavailableError``; nothing else changes.

Security posture:
- ``typesafe_sdk`` is imported lazily per call, never at import/construct time.
- The API key is resolved per call through ``VaultResolver`` (vault path, then
  ``TYPESAFE_API_KEY``) and passed explicitly as ``api_key=``. It is never put
  in ``os.environ`` and never appears in logs, telemetry or exception text
  (typed errors are raised outside ``except`` blocks, so no vendor exception
  rides the ``__cause__`` / ``__context__`` chain).
- ``base_url`` and ``model`` are always passed explicitly, so ambient
  ``TYPESAFE_BASE_URL`` / ``TYPESAFE_DEFAULT_MODEL`` can never steer egress.
- The SDK logs request/response BODIES at debug; its logger is silenced so
  memory text never reaches any log handler.
- Retries are bounded (``max_retries=2``); 429/529 surface as errors.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Mapping
from types import ModuleType
from typing import Any

from arcllm.classify import (
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
from arcllm.exceptions import ArcLLMConfigError
from arcllm.vault import VaultResolver

API_KEY_ENV = "TYPESAFE_API_KEY"
# Pinned model versions offered to operators (no "-latest": promotion config
# rejects unpinned names). An unlisted pinned version is still accepted.
MODELS: tuple[str, ...] = ("jev-1.13.0",)
DEFAULT_BASE_URL = "https://api.typesafe.ai"
_SDK_MODULE = "typesafe_sdk"
_MAX_RETRIES = 2

logger = logging.getLogger(__name__)


def _silence_sdk_logging() -> None:
    """Stop the SDK's request/response body logging from reaching any handler.

    Applied per call (after the SDK import, which may apply
    ``TYPESAFE_LOG_LEVEL``), so a later reconfiguration cannot re-open it for
    long. ``propagate=False`` also stops SDK child loggers at this node.
    """
    sdk_logger = logging.getLogger(_SDK_MODULE)
    sdk_logger.setLevel(logging.CRITICAL + 1)
    sdk_logger.propagate = False
    sdk_logger.disabled = True


def _question_to_wire(spec: ChoiceSpec | NoulSpec) -> dict[str, Any]:
    """The documented raw question dict — keeps us off the SDK's model classes."""
    if isinstance(spec, ChoiceSpec):
        criteria = {
            option: dict(desc) if isinstance(desc, Mapping) else desc
            for option, desc in spec.criteria.items()
        }
        return {"type": "choice", "instructions": spec.instructions, "criteria": criteria}
    wire: dict[str, Any] = {"type": "noul", "instructions": spec.instructions}
    if spec.criteria is not None:
        wire["criteria"] = dict(spec.criteria)
    return wire


def _map_answer(answer: Any) -> ChoiceResult | NoulResult:
    kind = getattr(answer, "type", None)
    if kind == "choice":
        return ChoiceResult(
            choice=answer.choice,
            confidence=answer.confidence,
            probabilities=dict(answer.probabilities),
        )
    if kind == "noul":
        return NoulResult(noul=answer.noul)
    raise TypeError("unknown answer type")


def _request_id(response: Any) -> str | None:
    """The response's request id, or ``None`` when the server sent none.

    The SDK's ``request_id`` property RAISES when the header is absent; the id
    is optional trace metadata, so its absence must not fail the verdict.
    """
    try:
        request_id = response.request_id
    except Exception:  # reason: SDK raises TypeSafeError for a missing header
        return None
    return request_id if isinstance(request_id, str) else None


def _map_response(response: Any) -> ClassificationResult:
    return ClassificationResult(
        model=response.model,
        answers={key: _map_answer(answer) for key, answer in response.answers.items()},
        request_id=_request_id(response),
        input_tokens=response.usage.input_tokens,
    )


class JevClassifier(ClassifierProvider):
    """TypeSafe Jev System One classifier for one pinned model."""

    def __init__(
        self,
        model: str,
        *,
        vault_resolver: VaultResolver | None = None,
        vault_path: str | None = None,
        api_key_env: str = API_KEY_ENV,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 10.0,
        transport: object | None = None,
    ) -> None:
        self._model = model
        self._vault = vault_resolver if vault_resolver is not None else VaultResolver(None)
        self._vault_path = vault_path
        self._api_key_env = api_key_env
        self._base_url = base_url
        self._timeout = timeout
        self._transport = transport

    @property
    def model_name(self) -> str:
        return self._model

    def check_available(self) -> None:
        """The SDK imports and a key resolves — the same steps ``classify`` takes
        first — without building a client or sending anything."""
        self._import_sdk()
        self._resolve_key()

    async def classify(self, request: ClassificationRequest) -> ClassificationResult:
        sdk = self._import_sdk()
        client = self._client(sdk, self._resolve_key())
        response = await self._system_one(sdk, client, request)
        return self._to_result(response)

    # -- steps ---------------------------------------------------------------

    def _import_sdk(self) -> ModuleType:
        try:
            # importlib keeps the SDK type-opaque: arcllm type-checks and
            # imports with the arcllm[jev] extra absent.
            return importlib.import_module(_SDK_MODULE)
        except ImportError:
            raise ArcLLMClassifierUnavailableError(
                self._model, "typesafe-sdk is not installed (install arcllm[jev])"
            ) from None

    def _resolve_key(self) -> str:
        try:
            return self._vault.resolve_api_key(self._api_key_env, self._vault_path)
        except ArcLLMConfigError:
            raise ArcLLMClassifierUnavailableError(
                self._model, f"no API key in the vault or '{self._api_key_env}'"
            ) from None

    def _client(self, sdk: ModuleType, api_key: str) -> Any:
        _silence_sdk_logging()
        try:
            return sdk.AsyncTypeSafeClient(
                api_key=api_key,
                model=self._model,
                base_url=self._base_url,
                timeout=self._timeout,
                retry=sdk.RetryPolicy(max_retries=_MAX_RETRIES),
                transport=self._transport,
            )
        except Exception as exc:  # reason: SDK rejects bad keys; its text may echo the key
            failure = type(exc).__name__
        raise ArcLLMClassifierError(f"jev client setup failed ({failure})")

    async def _system_one(
        self, sdk: ModuleType, client: Any, request: ClassificationRequest
    ) -> Any:
        questions = {key: _question_to_wire(spec) for key, spec in request.questions.items()}
        try:
            async with client:
                return await client.system_one(request.state, questions)
        except sdk.TypeSafeAPIError as exc:
            failure = f"status {exc.status}, request_id={exc.request_id}"
        except Exception as exc:  # reason: transport/timeout failures fail closed, typed
            failure = type(exc).__name__
        logger.warning("jev classify call failed: %s", failure)
        raise ArcLLMClassifierError(f"jev call failed ({failure})")

    def _to_result(self, response: Any) -> ClassificationResult:
        try:
            return _map_response(response)
        except Exception as exc:  # reason: any shape mismatch is a malformed response
            failure = type(exc).__name__
        raise ArcLLMClassifierError(f"malformed jev response ({failure})")


CLASSIFIER = JevClassifier

__all__ = ["API_KEY_ENV", "CLASSIFIER", "DEFAULT_BASE_URL", "JevClassifier"]
