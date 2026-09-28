"""Folder-scanned classifier drop-ins (ADR-032 pattern, SPEC-083).

A drop-in is discovered by being physically present under
``arcllm/classifiers/``. These tests point the scan root at a temp folder so
the real scan + import path runs against drop-ins the test controls.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

import arcllm.classifiers as classifiers
from arcllm import ArcLLMClassifierUnavailableError
from arcllm.exceptions import ArcLLMConfigError


@pytest.fixture
def drop_in_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Replace the scan root with an empty temp folder; forget imported fakes after."""
    monkeypatch.setattr(classifiers, "__path__", [str(tmp_path)])
    before = set(sys.modules)
    yield tmp_path
    for name in set(sys.modules) - before:
        if name.startswith(f"{classifiers.__name__}."):
            del sys.modules[name]


def test_absent_drop_in_is_unavailable_not_a_config_error(drop_in_root: Path) -> None:
    with pytest.raises(ArcLLMClassifierUnavailableError):
        classifiers.load_classifier("removed", "model-x")


def test_drop_in_without_classifier_export_is_a_config_error(drop_in_root: Path) -> None:
    (drop_in_root / "hollow.py").write_text("API_KEY_ENV = 'HOLLOW_KEY'\n")

    with pytest.raises(ArcLLMConfigError, match="does not export a ClassifierProvider"):
        classifiers.load_classifier("hollow", "model-x")


def test_drop_in_exporting_a_non_provider_is_a_config_error(drop_in_root: Path) -> None:
    (drop_in_root / "impostor.py").write_text("class CLASSIFIER:\n    pass\n")

    with pytest.raises(ArcLLMConfigError, match="does not export a ClassifierProvider"):
        classifiers.load_classifier("impostor", "model-x")


def test_key_listing_skips_drop_ins_that_declare_no_key(drop_in_root: Path) -> None:
    (drop_in_root / "keyed.py").write_text("API_KEY_ENV = 'KEYED_API_KEY'\n")
    (drop_in_root / "keyless.py").write_text("")
    (drop_in_root / "blank.py").write_text("API_KEY_ENV = ''\n")
    (drop_in_root / "_private.py").write_text("API_KEY_ENV = 'PRIVATE_KEY'\n")

    keys = classifiers.list_classifier_keys()

    assert [(k.provider, k.api_key_env, k.required) for k in keys] == [
        ("keyed", "KEYED_API_KEY", False)
    ]
    assert classifiers.classifier_key_envs() == frozenset({"KEYED_API_KEY"})
