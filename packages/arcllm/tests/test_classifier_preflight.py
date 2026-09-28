"""SPEC-083 — network-free classifier preflight (``ClassifierProvider.check_available``).

A caller that must audit egress BEFORE sending anything (the memory promotion
sweep) needs to learn "this classifier cannot serve" without a request: the
drop-in resolves, its optional SDK imports, and its key resolves through the same
``VaultResolver`` path ``classify`` uses. No client is built and nothing is sent.

The SDK stand-in here records client construction and calls, so "no request"
is asserted, not assumed.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from arcllm import ArcLLMClassifierUnavailableError, resolve_classifier
from arcllm.classifiers.jev import JevClassifier
from arcllm.classify import ClassificationRequest, ClassificationResult, ClassifierProvider
from arcllm.vault import VaultResolver

MODEL = "jev-1.13.0"
KEY = "tsk_preflight_SENTINEL"


class _RecordingSdk:
    """``typesafe_sdk`` stand-in: records every client built and every call made."""

    def __init__(self) -> None:
        self.clients: list[dict[str, Any]] = []

    def module(self) -> types.ModuleType:
        sdk = self

        class AsyncTypeSafeClient:
            def __init__(self, **kwargs: Any) -> None:
                sdk.clients.append(kwargs)

        module = types.ModuleType("typesafe_sdk")
        module.AsyncTypeSafeClient = AsyncTypeSafeClient  # type: ignore[attr-defined]  # reason: synthetic module
        module.RetryPolicy = lambda **kwargs: kwargs  # type: ignore[attr-defined]  # reason: synthetic module
        return module


@pytest.fixture(autouse=True)
def _no_ambient_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> _RecordingSdk:
    recorder = _RecordingSdk()
    monkeypatch.setitem(sys.modules, "typesafe_sdk", recorder.module())
    return recorder


class _NoPreflightProvider(ClassifierProvider):
    @property
    def model_name(self) -> str:
        return MODEL

    async def classify(self, request: ClassificationRequest) -> ClassificationResult:
        raise AssertionError("preflight must never classify")


def test_default_provider_preflight_is_a_no_op() -> None:
    _NoPreflightProvider().check_available()  # does not raise


def test_jev_preflight_with_sdk_and_env_key_passes_and_builds_no_client(
    sdk: _RecordingSdk, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", KEY)

    JevClassifier(MODEL).check_available()

    assert sdk.clients == []


def test_jev_preflight_without_sdk_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "typesafe_sdk", None)
    monkeypatch.setenv("TYPESAFE_API_KEY", KEY)

    with pytest.raises(ArcLLMClassifierUnavailableError, match="typesafe-sdk"):
        JevClassifier(MODEL).check_available()


def test_jev_preflight_without_key_is_unavailable_and_builds_no_client(
    sdk: _RecordingSdk,
) -> None:
    with pytest.raises(ArcLLMClassifierUnavailableError, match="API key"):
        JevClassifier(MODEL, vault_resolver=VaultResolver(None)).check_available()

    assert sdk.clients == []


def test_jev_preflight_key_error_never_carries_the_key(
    sdk: _RecordingSdk, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OTHER_KEY_ENV", KEY)

    with pytest.raises(ArcLLMClassifierUnavailableError) as caught:
        JevClassifier(MODEL, api_key_env="MISSING_KEY_ENV").check_available()

    assert KEY not in str(caught.value)


def test_resolved_named_drop_in_exposes_the_preflight(
    sdk: _RecordingSdk, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CUSTOM_TYPESAFE_ENV", KEY)

    classifier = resolve_classifier("jev", MODEL, api_key_env="CUSTOM_TYPESAFE_ENV")
    classifier.check_available()

    assert sdk.clients == []
