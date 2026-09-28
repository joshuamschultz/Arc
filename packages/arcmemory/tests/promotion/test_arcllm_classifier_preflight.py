"""SPEC-083 COMP-013/015 — ``ArcllmPromotionClassifier.ensure_available`` preflight.

The adapter answers "can this classifier serve?" without a request: it resolves the
named drop-in and runs the drop-in's network-free ``check_available`` (SDK import,
key resolution). An absent drop-in, SDK or key is ``ClassifierUnavailableError``.
The drop-in's ``classify`` is never called.
"""

from __future__ import annotations

from typing import Any

import arcllm
import arcllm.classifiers
import pytest
from arcllm import ClassificationRequest, ClassificationResult, ClassifierProvider

from arcmemory.promotion.arcllm_classifier import ArcllmPromotionClassifier
from arcmemory.promotion.classifier import ClassifierUnavailableError, PromotionClassifier

_MODEL = "jev-1.13.0"


class _DropIn(ClassifierProvider):
    """A drop-in whose preflight passes or reports unavailable, as scripted."""

    def __init__(self, *, unavailable: bool) -> None:
        self._unavailable = unavailable
        self.preflights = 0
        self.classified = 0

    @property
    def model_name(self) -> str:
        return _MODEL

    def check_available(self) -> None:
        self.preflights += 1
        if self._unavailable:
            raise arcllm.ArcLLMClassifierUnavailableError(_MODEL, "no API key configured")

    async def classify(self, request: ClassificationRequest) -> ClassificationResult:
        self.classified += 1
        raise AssertionError("preflight must never classify")


def _install(monkeypatch: pytest.MonkeyPatch, drop_in: _DropIn) -> list[dict[str, Any]]:
    lookups: list[dict[str, Any]] = []

    def _load(name: str, model: str, **key_coordinate: str) -> ClassifierProvider:
        lookups.append({"name": name, "model": model, **key_coordinate})
        return drop_in

    monkeypatch.setattr(arcllm.classifiers, "load_classifier", _load)
    return lookups


async def test_available_drop_in_passes_preflight_without_classifying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drop_in = _DropIn(unavailable=False)
    lookups = _install(monkeypatch, drop_in)
    classifier = ArcllmPromotionClassifier(
        "jev", _MODEL, 5.0, api_key_env="KEY_ENV", vault_path="secret/jev"
    )

    await classifier.ensure_available()

    assert drop_in.preflights == 1
    assert drop_in.classified == 0
    assert lookups == [
        {"name": "jev", "model": _MODEL, "api_key_env": "KEY_ENV", "vault_path": "secret/jev"}
    ]


async def test_unavailable_drop_in_maps_to_classifier_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drop_in = _DropIn(unavailable=True)
    _install(monkeypatch, drop_in)

    with pytest.raises(ClassifierUnavailableError):
        await ArcllmPromotionClassifier("jev", _MODEL, 5.0).ensure_available()

    assert drop_in.classified == 0


async def test_uninstalled_drop_in_maps_to_classifier_unavailable() -> None:
    with pytest.raises(ClassifierUnavailableError):
        await ArcllmPromotionClassifier("no_such_classifier", _MODEL, 5.0).ensure_available()


def test_adapter_satisfies_the_protocol_surface() -> None:
    classifier: PromotionClassifier = ArcllmPromotionClassifier("jev", _MODEL, 5.0)

    assert callable(classifier.ensure_available)
