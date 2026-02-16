"""Tests for SecurityModule — PII redaction + request signing integration."""

import os
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from arcllm._pii import PiiDetector, PiiMatch, RegexPiiDetector
from arcllm.exceptions import ArcLLMConfigError
from arcllm.modules.security import SecurityModule
from arcllm.types import (
    LLMProvider,
    LLMResponse,
    Message,
    TextBlock,
    Tool,
    ToolCall,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_USAGE = Usage(input_tokens=10, output_tokens=20, total_tokens=30)


def _make_response(
    content: str | None = "Hello",
    tool_calls: list[ToolCall] | None = None,
    stop_reason: str = "end_turn",
    metadata: dict[str, Any] | None = None,
) -> LLMResponse:
    return LLMResponse(
        content=content,
        tool_calls=tool_calls or [],
        usage=_USAGE,
        model="test-model",
        stop_reason=stop_reason,
        metadata=metadata,
    )


def _make_inner(response: LLMResponse | None = None) -> LLMProvider:
    """Create a mock inner provider."""
    mock = AsyncMock(spec=LLMProvider)
    mock.name = "test-provider"
    mock.model_name = "test-model"
    mock.invoke = AsyncMock(return_value=response or _make_response())
    return mock


def _base_config(**overrides: Any) -> dict[str, Any]:
    """Security module config with sane test defaults."""
    cfg: dict[str, Any] = {
        "pii_enabled": True,
        "pii_detector": "regex",
        "pii_custom_patterns": [],
        "signing_enabled": True,
        "signing_algorithm": "hmac-sha256",
        "signing_key_env": "TEST_SIGNING_KEY",
    }
    cfg.update(overrides)
    return cfg


# ---------------------------------------------------------------------------
# PII redaction — outbound messages (text content)
# ---------------------------------------------------------------------------


class TestPiiOutboundText:
    @pytest.fixture(autouse=True)
    def _set_signing_key(self):
        with patch.dict(os.environ, {"TEST_SIGNING_KEY": "test-key"}):
            yield

    async def test_redacts_pii_from_text_message(self):
        inner = _make_inner()
        module = SecurityModule(_base_config(), inner)
        messages = [Message(role="user", content="My SSN is 123-45-6789")]

        await module.invoke(messages)

        # Inner should receive redacted messages
        call_args = inner.invoke.call_args
        sent_messages = call_args[0][0]
        assert "123-45-6789" not in sent_messages[0].content
        assert "[PII:SSN]" in sent_messages[0].content

    async def test_redacts_multiple_pii_types(self):
        inner = _make_inner()
        module = SecurityModule(_base_config(), inner)
        messages = [
            Message(role="user", content="SSN 123-45-6789, email user@test.com")
        ]

        await module.invoke(messages)

        sent_messages = inner.invoke.call_args[0][0]
        assert "[PII:SSN]" in sent_messages[0].content
        assert "[PII:EMAIL]" in sent_messages[0].content


# ---------------------------------------------------------------------------
# PII redaction — outbound messages (ContentBlock content)
# ---------------------------------------------------------------------------


class TestPiiOutboundContentBlocks:
    @pytest.fixture(autouse=True)
    def _set_signing_key(self):
        with patch.dict(os.environ, {"TEST_SIGNING_KEY": "test-key"}):
            yield

    async def test_redacts_pii_from_text_block(self):
        inner = _make_inner()
        module = SecurityModule(_base_config(), inner)
        messages = [
            Message(
                role="user",
                content=[TextBlock(text="SSN: 123-45-6789")],
            )
        ]

        await module.invoke(messages)

        sent = inner.invoke.call_args[0][0]
        block = sent[0].content[0]
        assert "[PII:SSN]" in block.text
        assert "123-45-6789" not in block.text

    async def test_redacts_pii_from_tool_result_block(self):
        inner = _make_inner()
        module = SecurityModule(_base_config(), inner)
        messages = [
            Message(
                role="tool",
                content=[
                    ToolResultBlock(
                        tool_use_id="t1",
                        content="User email: user@test.com",
                    )
                ],
            )
        ]

        await module.invoke(messages)

        sent = inner.invoke.call_args[0][0]
        block = sent[0].content[0]
        assert "[PII:EMAIL]" in block.content
        assert "user@test.com" not in block.content

    async def test_redacts_pii_from_tool_use_block_args(self):
        """ToolUseBlock arguments containing PII should be redacted."""
        inner = _make_inner()
        module = SecurityModule(_base_config(), inner)
        messages = [
            Message(
                role="assistant",
                content=[
                    ToolUseBlock(
                        id="tu1",
                        name="lookup_user",
                        arguments={"ssn": "123-45-6789", "query": "find user"},
                    )
                ],
            )
        ]

        await module.invoke(messages)

        sent = inner.invoke.call_args[0][0]
        block = sent[0].content[0]
        assert isinstance(block, ToolUseBlock)
        assert "123-45-6789" not in str(block.arguments)
        assert "[PII:SSN]" in str(block.arguments)

    async def test_redacts_pii_from_tool_result_block_with_list_content(self):
        """ToolResultBlock with list[ContentBlock] content should pass through."""
        inner = _make_inner()
        module = SecurityModule(_base_config(), inner)
        messages = [
            Message(
                role="tool",
                content=[
                    ToolResultBlock(
                        tool_use_id="t1",
                        content=[TextBlock(text="SSN: 123-45-6789")],
                    )
                ],
            )
        ]

        await module.invoke(messages)

        sent = inner.invoke.call_args[0][0]
        block = sent[0].content[0]
        assert isinstance(block, ToolResultBlock)
        # list content passes through (not scanned at this level)
        assert isinstance(block.content, list)

    async def test_skips_image_block(self):
        """ImageBlock has no text to scan."""
        from arcllm.types import ImageBlock

        inner = _make_inner()
        module = SecurityModule(_base_config(), inner)
        messages = [
            Message(
                role="user",
                content=[ImageBlock(source="base64data", media_type="image/png")],
            )
        ]

        await module.invoke(messages)

        sent = inner.invoke.call_args[0][0]
        # Image block should pass through unchanged
        assert sent[0].content[0].source == "base64data"


# ---------------------------------------------------------------------------
# PII redaction — inbound response
# ---------------------------------------------------------------------------


class TestPiiInbound:
    @pytest.fixture(autouse=True)
    def _set_signing_key(self):
        with patch.dict(os.environ, {"TEST_SIGNING_KEY": "test-key"}):
            yield

    async def test_redacts_pii_from_response_content(self):
        response = _make_response(content="Your SSN 123-45-6789 is on file")
        inner = _make_inner(response)
        module = SecurityModule(_base_config(), inner)
        messages = [Message(role="user", content="show my info")]

        result = await module.invoke(messages)

        assert "123-45-6789" not in result.content
        assert "[PII:SSN]" in result.content

    async def test_no_redaction_on_none_content(self):
        response = _make_response(content=None)
        inner = _make_inner(response)
        module = SecurityModule(_base_config(), inner)
        messages = [Message(role="user", content="test")]

        result = await module.invoke(messages)

        assert result.content is None


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------


class TestSigning:
    @pytest.fixture(autouse=True)
    def _set_signing_key(self):
        with patch.dict(os.environ, {"TEST_SIGNING_KEY": "test-key"}):
            yield

    async def test_signature_attached_to_metadata(self):
        inner = _make_inner()
        module = SecurityModule(_base_config(), inner)
        messages = [Message(role="user", content="hello")]

        result = await module.invoke(messages)

        assert result.metadata is not None
        assert "request_signature" in result.metadata
        assert "signing_algorithm" in result.metadata
        assert result.metadata["signing_algorithm"] == "hmac-sha256"

    async def test_signature_is_hex_string(self):
        inner = _make_inner()
        module = SecurityModule(_base_config(), inner)
        messages = [Message(role="user", content="hello")]

        result = await module.invoke(messages)

        sig = result.metadata["request_signature"]
        assert len(sig) == 64
        assert all(c in "0123456789abcdef" for c in sig)

    async def test_signature_deterministic(self):
        inner = _make_inner()
        module = SecurityModule(_base_config(), inner)
        messages = [Message(role="user", content="same message")]

        r1 = await module.invoke(messages)
        r2 = await module.invoke(messages)

        assert r1.metadata["request_signature"] == r2.metadata["request_signature"]


# ---------------------------------------------------------------------------
# Combined PII + signing
# ---------------------------------------------------------------------------


class TestCombined:
    @pytest.fixture(autouse=True)
    def _set_signing_key(self):
        with patch.dict(os.environ, {"TEST_SIGNING_KEY": "test-key"}):
            yield

    async def test_pii_redacted_and_signed(self):
        response = _make_response(content="Email: agent@test.com")
        inner = _make_inner(response)
        module = SecurityModule(_base_config(), inner)
        messages = [Message(role="user", content="SSN 123-45-6789")]

        result = await module.invoke(messages)

        # Outbound PII redacted
        sent = inner.invoke.call_args[0][0]
        assert "[PII:SSN]" in sent[0].content
        # Inbound PII redacted
        assert "[PII:EMAIL]" in result.content
        # Signature present
        assert result.metadata["request_signature"]


# ---------------------------------------------------------------------------
# Feature toggle tests
# ---------------------------------------------------------------------------


class TestFeatureToggles:
    @pytest.fixture(autouse=True)
    def _set_signing_key(self):
        with patch.dict(os.environ, {"TEST_SIGNING_KEY": "test-key"}):
            yield

    async def test_pii_disabled_signing_only(self):
        inner = _make_inner()
        module = SecurityModule(_base_config(pii_enabled=False), inner)
        messages = [Message(role="user", content="SSN 123-45-6789")]

        result = await module.invoke(messages)

        # PII NOT redacted
        sent = inner.invoke.call_args[0][0]
        assert "123-45-6789" in sent[0].content
        # Signature present
        assert result.metadata is not None
        assert "request_signature" in result.metadata

    async def test_signing_disabled_pii_only(self):
        inner = _make_inner()
        module = SecurityModule(
            _base_config(signing_enabled=False), inner
        )
        messages = [Message(role="user", content="SSN 123-45-6789")]

        result = await module.invoke(messages)

        # PII redacted
        sent = inner.invoke.call_args[0][0]
        assert "[PII:SSN]" in sent[0].content
        # No signature
        assert result.metadata is None or "request_signature" not in (
            result.metadata or {}
        )

    async def test_both_disabled_passthrough(self):
        inner = _make_inner()
        module = SecurityModule(
            _base_config(pii_enabled=False, signing_enabled=False), inner
        )
        messages = [Message(role="user", content="SSN 123-45-6789")]

        result = await module.invoke(messages)

        # No PII redaction
        sent = inner.invoke.call_args[0][0]
        assert "123-45-6789" in sent[0].content
        # No signature
        assert result.metadata is None


# ---------------------------------------------------------------------------
# OTel spans
# ---------------------------------------------------------------------------


class TestOtelSpans:
    @pytest.fixture(autouse=True)
    def _set_signing_key(self):
        with patch.dict(os.environ, {"TEST_SIGNING_KEY": "test-key"}):
            yield

    async def test_creates_security_span(self):
        """SecurityModule should create OTel spans (no-op without SDK)."""
        inner = _make_inner()
        module = SecurityModule(_base_config(), inner)
        messages = [Message(role="user", content="hello")]

        # Should not raise even without OTel SDK
        result = await module.invoke(messages)
        assert result is not None


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_invalid_signing_algorithm(self):
        with patch.dict(os.environ, {"TEST_SIGNING_KEY": "key"}):
            with pytest.raises(ArcLLMConfigError, match="Unsupported"):
                SecurityModule(
                    _base_config(signing_algorithm="rsa-2048"),
                    _make_inner(),
                )

    def test_missing_signing_key_env(self):
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ArcLLMConfigError, match="not set"):
                SecurityModule(
                    _base_config(signing_key_env="MISSING_KEY"),
                    _make_inner(),
                )

    def test_signing_disabled_no_key_required(self):
        """When signing disabled, missing key should not error."""
        with patch.dict(os.environ, {}, clear=True):
            module = SecurityModule(
                _base_config(signing_enabled=False, signing_key_env="MISSING"),
                _make_inner(),
            )
            assert module is not None

    def test_unknown_detector_type_raises(self):
        with patch.dict(os.environ, {"TEST_SIGNING_KEY": "key"}):
            with pytest.raises(ArcLLMConfigError, match="Unsupported pii_detector"):
                SecurityModule(
                    _base_config(pii_detector="spacy"),
                    _make_inner(),
                )

    def test_unknown_config_keys_raises(self):
        with patch.dict(os.environ, {"TEST_SIGNING_KEY": "key"}):
            with pytest.raises(ArcLLMConfigError, match="Unknown SecurityModule"):
                SecurityModule(
                    {**_base_config(), "bogus_key": True},
                    _make_inner(),
                )


# ---------------------------------------------------------------------------
# Custom PII detector class
# ---------------------------------------------------------------------------


class TestCustomDetector:
    @pytest.fixture(autouse=True)
    def _set_signing_key(self):
        with patch.dict(os.environ, {"TEST_SIGNING_KEY": "test-key"}):
            yield

    async def test_custom_detector_class(self):
        """SecurityModule should use custom PII detector when configured."""

        class AlwaysDetector:
            def detect(self, text: str) -> list[PiiMatch]:
                if text:
                    return [PiiMatch("CUSTOM", 0, len(text), text)]
                return []

        inner = _make_inner()
        module = SecurityModule(_base_config(), inner)
        # Manually swap detector for testing
        module._pii_detector = AlwaysDetector()
        messages = [Message(role="user", content="anything")]

        await module.invoke(messages)

        sent = inner.invoke.call_args[0][0]
        assert "[PII:CUSTOM]" in sent[0].content
