"""Tests for the allowlisted `pii_detector_class` loader (completes D-093/FR-13).

Spec 015 FR-19..FR-22. Mirrors vault.py's VaultResolver.from_config allowlist
pattern (ADR-427): the prefix-allowlist gate must run BEFORE import_module,
because import executes top-level module code (import-time RCE) — checking
after import would be too late (SDD Research Insight, ASI04/ASI05).
"""

import os
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from arcllm._pii import PiiMatch
from arcllm.exceptions import ArcLLMConfigError
from arcllm.modules.security import SecurityModule
from arcllm.types import LLMProvider, LLMResponse, Message, Usage

_USAGE = Usage(input_tokens=10, output_tokens=20, total_tokens=30)


def _make_response() -> LLMResponse:
    return LLMResponse(content="hello", usage=_USAGE, model="test-model", stop_reason="end_turn")


def _make_inner() -> LLMProvider:
    mock = AsyncMock(spec=LLMProvider)
    mock.name = "test-provider"
    mock.model_name = "test-model"
    mock.invoke = AsyncMock(return_value=_make_response())
    return mock


def _base_config(**overrides: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "pii_enabled": True,
        "signing_enabled": False,
    }
    cfg.update(overrides)
    return cfg


class _AlwaysDetector:
    """A trivial custom PiiDetector used as a stand-in for a licensed detector."""

    def detect(self, text: str) -> list[PiiMatch]:
        if text:
            return [PiiMatch(pii_type="CUSTOM", start=0, end=len(text), matched_text=text)]
        return []


class _NoDetectMethod:
    """Lacks .detect() — should fail the PiiDetector protocol check."""


# Make the fixture classes importable under an allowlisted prefix by
# attaching them to this test module, which lives under `tests.*` — NOT
# allowlisted. Real allowlisted-path tests monkeypatch the allowlist
# tuple directly so we can exercise the happy path without needing a
# real package under arcllm.*/arcagent.*/arcpii.*.


class TestValidDetectorClassLoads:
    def test_custom_detector_class_loads_and_runs(self):
        with patch(
            "arcllm.modules.security._ALLOWED_DETECTOR_PREFIXES",
            ("tests.test_pii_loader",),
        ):
            module = SecurityModule(
                _base_config(pii_detector_class="tests.test_pii_loader:_AlwaysDetector"),
                _make_inner(),
            )
        assert isinstance(module._pii_detector, _AlwaysDetector)

    async def test_custom_detector_class_used_in_invoke(self):
        with patch(
            "arcllm.modules.security._ALLOWED_DETECTOR_PREFIXES",
            ("tests.test_pii_loader",),
        ):
            inner = _make_inner()
            module = SecurityModule(
                _base_config(pii_detector_class="tests.test_pii_loader:_AlwaysDetector"),
                inner,
            )
            messages = [Message(role="user", content="anything")]
            await module.invoke(messages)
            sent = inner.invoke.call_args[0][0]
            assert "[PII:CUSTOM]" in sent[0].content


class TestInvalidFormat:
    def test_missing_colon_raises(self):
        with pytest.raises(ArcLLMConfigError, match="module:Class"):
            SecurityModule(_base_config(pii_detector_class="not_a_valid_ref"), _make_inner())

    def test_file_path_rejected(self):
        """Only 'module:Class' absolute refs accepted — no arbitrary-file import."""
        with pytest.raises(ArcLLMConfigError, match="allowlist"):
            SecurityModule(
                _base_config(pii_detector_class="/etc/passwd:Evil"),
                _make_inner(),
            )

    def test_relative_dotted_name_rejected(self):
        with pytest.raises(ArcLLMConfigError, match="allowlist"):
            SecurityModule(
                _base_config(pii_detector_class=".relative.module:Evil"),
                _make_inner(),
            )


class TestNonAllowlistedModuleRejected:
    def test_non_allowlisted_prefix_raises(self):
        with pytest.raises(ArcLLMConfigError, match="allowlist"):
            SecurityModule(
                _base_config(pii_detector_class="os:system"),
                _make_inner(),
            )

    def test_non_allowlisted_ref_never_calls_import_module(self):
        """Prefix check must run BEFORE import_module (import-time RCE guard)."""
        with patch(
            "arcllm.modules.security.importlib.import_module",
            side_effect=AssertionError("import_module must not be called for a rejected ref"),
        ):
            with pytest.raises(ArcLLMConfigError, match="allowlist"):
                SecurityModule(
                    _base_config(pii_detector_class="os:system"),
                    _make_inner(),
                )


class TestMissingModuleOrClass:
    def test_missing_module_raises(self):
        with patch(
            "arcllm.modules.security._ALLOWED_DETECTOR_PREFIXES",
            ("arcllm.nonexistent_pii_module_xyz",),
        ):
            with pytest.raises(ArcLLMConfigError, match="not installed"):
                SecurityModule(
                    _base_config(pii_detector_class="arcllm.nonexistent_pii_module_xyz:Detector"),
                    _make_inner(),
                )

    def test_missing_class_raises(self):
        with patch(
            "arcllm.modules.security._ALLOWED_DETECTOR_PREFIXES",
            ("arcllm._pii",),
        ):
            with pytest.raises(ArcLLMConfigError, match="not found"):
                SecurityModule(
                    _base_config(pii_detector_class="arcllm._pii:NoSuchClass"),
                    _make_inner(),
                )


class TestMissingDetectMethod:
    def test_class_without_detect_raises(self):
        with patch(
            "arcllm.modules.security._ALLOWED_DETECTOR_PREFIXES",
            ("tests.test_pii_loader",),
        ):
            with pytest.raises(ArcLLMConfigError, match="does not implement"):
                SecurityModule(
                    _base_config(pii_detector_class="tests.test_pii_loader:_NoDetectMethod"),
                    _make_inner(),
                )


class TestDetectorClassOverridesDetectorString:
    def test_pii_detector_class_wins_over_pii_detector(self):
        with patch(
            "arcllm.modules.security._ALLOWED_DETECTOR_PREFIXES",
            ("tests.test_pii_loader",),
        ):
            module = SecurityModule(
                _base_config(
                    pii_detector="regex",
                    pii_detector_class="tests.test_pii_loader:_AlwaysDetector",
                ),
                _make_inner(),
            )
        assert isinstance(module._pii_detector, _AlwaysDetector)


class TestRegressionHardRejectRemoved:
    """FR-22: non-'regex' pii_detector string values are no longer auto-rejected."""

    def test_non_regex_pii_detector_string_no_longer_raises(self):
        with patch.dict(os.environ, {}, clear=True):
            module = SecurityModule(_base_config(pii_detector="spacy"), _make_inner())
        # Falls through to the built-in RegexPiiDetector — no ArcLLMConfigError.
        from arcllm._pii import RegexPiiDetector

        assert isinstance(module._pii_detector, RegexPiiDetector)
