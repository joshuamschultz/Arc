"""Tests for GuardrailsModule — structural output validation (OWASP LLM05).

Spec 015 (Content Guardrails) FR-23..FR-30. Structural only (schema/regex/
length/stop-list) — semantic guardrails are explicitly out of scope (ADR-429).
"""

import time
from typing import Any, ClassVar
from unittest.mock import AsyncMock

import pytest

from arcllm.exceptions import ArcLLMConfigError, ArcLLMGuardrailError
from arcllm.modules.guardrails import GuardrailsModule, Violation
from arcllm.types import LLMProvider, LLMResponse, Message, Usage

_USAGE = Usage(input_tokens=10, output_tokens=20, total_tokens=30)


def _make_response(content: Any = "hello", metadata: dict[str, Any] | None = None) -> LLMResponse:
    return LLMResponse(
        content=content,
        usage=_USAGE,
        model="test-model",
        stop_reason="end_turn",
        metadata=metadata,
    )


def _make_inner(response: LLMResponse | None = None) -> LLMProvider:
    mock = AsyncMock(spec=LLMProvider)
    mock.name = "test-provider"
    mock.model_name = "test-model"
    mock.invoke = AsyncMock(return_value=response or _make_response())
    return mock


def _base_config(**overrides: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {"enforcement": "block"}
    cfg.update(overrides)
    return cfg


# ---------------------------------------------------------------------------
# JSON schema conformance
# ---------------------------------------------------------------------------


class TestJsonSchema:
    _SCHEMA: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }

    async def test_conforming_json_no_violation(self):
        inner = _make_inner(_make_response(content='{"name": "bob"}'))
        module = GuardrailsModule(_base_config(json_schema=self._SCHEMA), inner)
        result = await module.invoke([Message(role="user", content="hi")])
        assert result.content == '{"name": "bob"}'

    async def test_non_conforming_json_raises_in_block_mode(self):
        inner = _make_inner(_make_response(content='{"age": 5}'))
        module = GuardrailsModule(_base_config(json_schema=self._SCHEMA), inner)
        with pytest.raises(ArcLLMGuardrailError) as exc_info:
            await module.invoke([Message(role="user", content="hi")])
        assert any(v.rule == "json_schema" for v in exc_info.value.violations)

    async def test_non_json_content_flagged(self):
        inner = _make_inner(_make_response(content="not json at all"))
        module = GuardrailsModule(_base_config(json_schema=self._SCHEMA), inner)
        with pytest.raises(ArcLLMGuardrailError) as exc_info:
            await module.invoke([Message(role="user", content="hi")])
        assert any(v.rule == "json_schema" for v in exc_info.value.violations)

    async def test_warn_mode_flags_non_conforming_json_in_metadata(self):
        inner = _make_inner(_make_response(content='{"age": 5}'))
        module = GuardrailsModule(
            _base_config(enforcement="warn", json_schema=self._SCHEMA), inner
        )
        result = await module.invoke([Message(role="user", content="hi")])
        assert result.metadata is not None
        assert "guardrail_violations" in result.metadata


# ---------------------------------------------------------------------------
# Regex allow/deny
# ---------------------------------------------------------------------------


class TestAllowDenyPatterns:
    async def test_deny_pattern_hit_raises(self):
        inner = _make_inner(_make_response(content="this contains BADWORD in it"))
        module = GuardrailsModule(_base_config(deny_patterns=["BADWORD"]), inner)
        with pytest.raises(ArcLLMGuardrailError) as exc_info:
            await module.invoke([Message(role="user", content="hi")])
        assert any(v.rule == "deny_pattern" for v in exc_info.value.violations)

    async def test_deny_pattern_miss_no_violation(self):
        inner = _make_inner(_make_response(content="perfectly clean text"))
        module = GuardrailsModule(_base_config(deny_patterns=["BADWORD"]), inner)
        result = await module.invoke([Message(role="user", content="hi")])
        assert result.content == "perfectly clean text"

    async def test_allow_pattern_miss_raises(self):
        inner = _make_inner(_make_response(content="unrelated content"))
        module = GuardrailsModule(_base_config(allow_patterns=[r"^ALLOWED:"]), inner)
        with pytest.raises(ArcLLMGuardrailError) as exc_info:
            await module.invoke([Message(role="user", content="hi")])
        assert any(v.rule == "allow_pattern" for v in exc_info.value.violations)

    async def test_allow_pattern_hit_no_violation(self):
        inner = _make_inner(_make_response(content="ALLOWED: this is fine"))
        module = GuardrailsModule(_base_config(allow_patterns=[r"^ALLOWED:"]), inner)
        result = await module.invoke([Message(role="user", content="hi")])
        assert result.content == "ALLOWED: this is fine"

    def test_invalid_deny_pattern_raises_at_construction(self):
        with pytest.raises(ArcLLMConfigError, match="Invalid regex"):
            GuardrailsModule(_base_config(deny_patterns=["[invalid"]), _make_inner())


# ---------------------------------------------------------------------------
# Max length
# ---------------------------------------------------------------------------


class TestMaxLength:
    async def test_over_length_flagged(self):
        inner = _make_inner(_make_response(content="x" * 100))
        module = GuardrailsModule(_base_config(max_length=50), inner)
        with pytest.raises(ArcLLMGuardrailError) as exc_info:
            await module.invoke([Message(role="user", content="hi")])
        assert any(v.rule == "max_length" for v in exc_info.value.violations)

    async def test_under_length_no_violation(self):
        inner = _make_inner(_make_response(content="short"))
        module = GuardrailsModule(_base_config(max_length=50), inner)
        result = await module.invoke([Message(role="user", content="hi")])
        assert result.content == "short"

    async def test_zero_max_length_means_uncapped(self):
        inner = _make_inner(_make_response(content="x" * 10_000))
        module = GuardrailsModule(_base_config(max_length=0), inner)
        result = await module.invoke([Message(role="user", content="hi")])
        assert len(result.content) == 10_000


# ---------------------------------------------------------------------------
# Banned content stop-list
# ---------------------------------------------------------------------------


class TestBannedContent:
    async def test_banned_phrase_hit_raises(self):
        inner = _make_inner(_make_response(content="this text has a bad phrase in it"))
        module = GuardrailsModule(_base_config(banned_content=["bad phrase"]), inner)
        with pytest.raises(ArcLLMGuardrailError) as exc_info:
            await module.invoke([Message(role="user", content="hi")])
        assert any(v.rule == "banned_content" for v in exc_info.value.violations)

    async def test_banned_phrase_case_insensitive(self):
        inner = _make_inner(_make_response(content="THIS HAS A BAD PHRASE here"))
        module = GuardrailsModule(_base_config(banned_content=["bad phrase"]), inner)
        with pytest.raises(ArcLLMGuardrailError):
            await module.invoke([Message(role="user", content="hi")])

    async def test_no_banned_phrase_no_violation(self):
        inner = _make_inner(_make_response(content="totally clean"))
        module = GuardrailsModule(_base_config(banned_content=["bad phrase"]), inner)
        result = await module.invoke([Message(role="user", content="hi")])
        assert result.content == "totally clean"

    async def test_banned_phrase_beyond_scan_cap_still_caught(self):
        """M1: banned_content is a plain substring check (no ReDoS risk) —
        it must never be silently truncated the way operator regex is."""
        from arcllm.modules.guardrails import _MAX_SCAN_LENGTH

        content = ("x" * (_MAX_SCAN_LENGTH + 500)) + "bad phrase"
        inner = _make_inner(_make_response(content=content))
        module = GuardrailsModule(_base_config(banned_content=["bad phrase"]), inner)
        with pytest.raises(ArcLLMGuardrailError) as exc_info:
            await module.invoke([Message(role="user", content="hi")])
        assert any(v.rule == "banned_content" for v in exc_info.value.violations)


# ---------------------------------------------------------------------------
# Enforcement modes
# ---------------------------------------------------------------------------


class TestEnforcement:
    async def test_block_raises_arcllm_guardrail_error(self):
        inner = _make_inner(_make_response(content="x" * 100))
        module = GuardrailsModule(_base_config(enforcement="block", max_length=10), inner)
        with pytest.raises(ArcLLMGuardrailError):
            await module.invoke([Message(role="user", content="hi")])

    async def test_warn_flags_and_continues(self):
        inner = _make_inner(_make_response(content="x" * 100))
        module = GuardrailsModule(_base_config(enforcement="warn", max_length=10), inner)
        result = await module.invoke([Message(role="user", content="hi")])
        assert result.content == "x" * 100
        assert result.metadata["guardrail_violations"][0]["rule"] == "max_length"

    def test_invalid_enforcement_raises(self):
        with pytest.raises(ArcLLMConfigError, match="enforcement"):
            GuardrailsModule(_base_config(enforcement="ignore"), _make_inner())


# ---------------------------------------------------------------------------
# Content shapes: list[ContentBlock], None
# ---------------------------------------------------------------------------


class TestContentShapes:
    """LLMResponse.content is str | None (never list[ContentBlock] — that
    shape belongs to Message.content, the request side, not the response).
    """

    async def test_plain_string_content_passes_clean_checks(self):
        response = _make_response(content="everything here is perfectly clean")
        inner = _make_inner(response)
        module = GuardrailsModule(_base_config(banned_content=["forbidden"]), inner)
        result = await module.invoke([Message(role="user", content="hi")])
        assert result is response

    async def test_none_content_no_checks_fire(self):
        response = _make_response(content=None)
        inner = _make_inner(response)
        module = GuardrailsModule(_base_config(max_length=5, banned_content=["x"]), inner)
        result = await module.invoke([Message(role="user", content="hi")])
        assert result.content is None


# ---------------------------------------------------------------------------
# Violation dataclass
# ---------------------------------------------------------------------------


class TestViolationDataclass:
    def test_violation_fields(self):
        v = Violation(rule="max_length", detail="content too long")
        assert v.rule == "max_length"
        assert v.detail == "content too long"


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_unknown_config_keys_raise(self):
        with pytest.raises(ArcLLMConfigError, match="Unknown GuardrailsModule"):
            GuardrailsModule({"bogus_key": True}, _make_inner())


# ---------------------------------------------------------------------------
# Span attributes
# ---------------------------------------------------------------------------


class TestSpanAttributes:
    async def test_span_does_not_raise_without_otel_sdk(self):
        inner = _make_inner(_make_response(content="clean"))
        module = GuardrailsModule(_base_config(), inner)
        result = await module.invoke([Message(role="user", content="hi")])
        assert result is not None


# ---------------------------------------------------------------------------
# Research Insight: jsonschema-optional determinism (SDD finding #3)
# ---------------------------------------------------------------------------


class TestJsonSchemaExtraGating:
    def test_json_schema_requires_extra_when_lib_absent(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def _fake_import(name, *args, **kwargs):
            if name == "jsonschema":
                raise ImportError("No module named 'jsonschema'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _fake_import)

        with pytest.raises(ArcLLMConfigError, match="guardrails-schema"):
            GuardrailsModule(
                _base_config(json_schema={"type": "object"}),
                _make_inner(),
            )

    def test_json_schema_check_deterministic_when_lib_present(self):
        """jsonschema is present in this dev environment — schema check must run."""
        module = GuardrailsModule(_base_config(json_schema={"type": "object"}), _make_inner())
        assert module is not None


# ---------------------------------------------------------------------------
# Research Insight: ReDoS guard — deny/allow scan is length-capped
# ---------------------------------------------------------------------------


class TestReDosGuard:
    async def test_deny_pattern_match_within_cap_still_detected(self):
        from arcllm.modules.guardrails import _MAX_SCAN_LENGTH

        content = "BADWORD" + ("x" * (_MAX_SCAN_LENGTH // 2))
        inner = _make_inner(_make_response(content=content))
        module = GuardrailsModule(_base_config(deny_patterns=["BADWORD"]), inner)
        with pytest.raises(ArcLLMGuardrailError):
            await module.invoke([Message(role="user", content="hi")])

    async def test_deny_pattern_beyond_cap_not_scanned(self):
        """Documented limitation: content beyond the cap is not regex-scanned."""
        from arcllm.modules.guardrails import _MAX_SCAN_LENGTH

        content = ("x" * (_MAX_SCAN_LENGTH + 1000)) + "BADWORD"
        inner = _make_inner(_make_response(content=content))
        module = GuardrailsModule(_base_config(deny_patterns=["BADWORD"]), inner)
        result = await module.invoke([Message(role="user", content="hi")])
        assert result.content == content

    async def test_catastrophic_pattern_bounded_by_cap(self):
        """A classic catastrophic-backtracking deny_pattern must not stall a worker.

        The response embeds a genuinely explosive `(a+)+$`-style substring
        (500 'a' characters followed by a mismatch) far beyond the scan
        cap. Without the cap this specific substring would take longer
        than is practical to wait for; with the cap, the pathological
        substring is never reached by the regex engine at all.
        """
        from arcllm.modules.guardrails import _MAX_SCAN_LENGTH

        # Pad well PAST the cap boundary so truncation excludes the
        # pathological run entirely — this is the property being tested.
        safe_prefix = "clean response text. " * 200
        assert len(safe_prefix) > _MAX_SCAN_LENGTH
        pathological_tail = ("a" * 500) + "!"
        content = safe_prefix + pathological_tail

        inner = _make_inner(_make_response(content=content))
        module = GuardrailsModule(_base_config(deny_patterns=[r"(a+)+$"]), inner)

        start = time.monotonic()
        result = await module.invoke([Message(role="user", content="hi")])
        elapsed = time.monotonic() - start

        assert elapsed < 2.0
        assert result.content == content
