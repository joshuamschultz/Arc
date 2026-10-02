"""Item 9 — a drop-in declares the pinned model names an operator may choose."""

from __future__ import annotations

import pytest

import arcllm
import arcllm.classifiers as classifiers
from arcllm.classifiers.jev import MODELS
from arcllm.classify import ArcLLMClassifierUnavailableError
from arcllm.exceptions import ArcLLMConfigError


def test_jev_models_are_pinned_names() -> None:
    assert "jev-1.13.0" in MODELS
    assert all(not m.endswith("-latest") for m in MODELS)


def test_list_classifier_models_returns_the_drop_in_list() -> None:
    assert arcllm.list_classifier_models("jev") == MODELS


def test_list_classifier_models_unknown_name_is_unavailable() -> None:
    with pytest.raises(ArcLLMClassifierUnavailableError):
        classifiers.list_classifier_models("no_such_classifier")


def test_list_classifier_models_rejects_import_paths() -> None:
    with pytest.raises(ArcLLMConfigError):
        classifiers.list_classifier_models("os:system")
