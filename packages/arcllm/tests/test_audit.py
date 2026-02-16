"""Tests for AuditModule — structured audit logging of LLM interactions."""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcllm.exceptions import ArcLLMConfigError
from arcllm.modules.audit import AuditModule
from arcllm.types import LLMProvider, LLMResponse, Message, Tool, ToolCall, Usage

_OK_RESPONSE = LLMResponse(
    content="Hello there!",
    usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
    model="test-model",
    stop_reason="end_turn",
)

_TOOL_RESPONSE = LLMResponse(
    content=None,
    tool_calls=[
        ToolCall(id="call_1", name="search", arguments={"query": "test"}),
        ToolCall(id="call_2", name="calc", arguments={"expr": "1+1"}),
    ],
    usage=Usage(input_tokens=20, output_tokens=10, total_tokens=30),
    model="test-model",
    stop_reason="tool_use",
)


def _make_inner(name: str = "test-provider"):
    inner = MagicMock(spec=LLMProvider)
    inner.name = name
    inner.model_name = "test-model"
    inner.validate_config.return_value = True
    inner.invoke = AsyncMock(return_value=_OK_RESPONSE)
    return inner


@pytest.fixture
def messages():
    return [
        Message(role="system", content="You are helpful."),
        Message(role="user", content="Hi"),
    ]


@pytest.fixture
def tools():
    return [
        Tool(name="search", description="Search the web", parameters={"type": "object"}),
    ]


# ---------------------------------------------------------------------------
# TestAuditModule — core behavior
# ---------------------------------------------------------------------------


class TestAuditModule:
    async def test_invoke_delegates_to_inner(self, messages):
        inner = _make_inner()
        module = AuditModule({}, inner)
        result = await module.invoke(messages)
        inner.invoke.assert_awaited_once_with(messages, None)
        assert result.content == "Hello there!"

    async def test_invoke_passes_tools_and_kwargs(self, messages, tools):
        inner = _make_inner()
        module = AuditModule({}, inner)
        await module.invoke(messages, tools=tools, max_tokens=100)
        inner.invoke.assert_awaited_once_with(messages, tools, max_tokens=100)

    async def test_returns_response_unchanged(self, messages):
        inner = _make_inner()
        module = AuditModule({}, inner)
        result = await module.invoke(messages)
        assert result is _OK_RESPONSE

    async def test_logs_basic_audit_fields(self, messages, caplog):
        inner = _make_inner("anthropic")
        module = AuditModule({}, inner)

        with caplog.at_level(logging.INFO, logger="arcllm.modules.audit"):
            await module.invoke(messages)

        assert "provider=anthropic" in caplog.text
        assert "model=test-model" in caplog.text
        assert "message_count=2" in caplog.text
        assert "stop_reason=end_turn" in caplog.text

    async def test_logs_tool_info_when_tools_provided(self, messages, tools, caplog):
        inner = _make_inner()
        module = AuditModule({}, inner)

        with caplog.at_level(logging.INFO, logger="arcllm.modules.audit"):
            await module.invoke(messages, tools=tools)

        assert "tools_provided=1" in caplog.text

    async def test_logs_no_tools_field_when_none(self, messages, caplog):
        inner = _make_inner()
        module = AuditModule({}, inner)

        with caplog.at_level(logging.INFO, logger="arcllm.modules.audit"):
            await module.invoke(messages)

        assert "tools_provided" not in caplog.text

    async def test_logs_tool_call_count(self, messages, caplog):
        inner = _make_inner()
        inner.invoke = AsyncMock(return_value=_TOOL_RESPONSE)
        module = AuditModule({}, inner)

        with caplog.at_level(logging.INFO, logger="arcllm.modules.audit"):
            await module.invoke(messages)

        assert "tool_calls=2" in caplog.text
        assert "stop_reason=tool_use" in caplog.text

    async def test_logs_content_length(self, messages, caplog):
        inner = _make_inner()
        module = AuditModule({}, inner)

        with caplog.at_level(logging.INFO, logger="arcllm.modules.audit"):
            await module.invoke(messages)

        # "Hello there!" = 12 chars
        assert "content_length=12" in caplog.text

    async def test_content_length_zero_when_none(self, messages, caplog):
        inner = _make_inner()
        inner.invoke = AsyncMock(return_value=_TOOL_RESPONSE)
        module = AuditModule({}, inner)

        with caplog.at_level(logging.INFO, logger="arcllm.modules.audit"):
            await module.invoke(messages)

        assert "content_length=0" in caplog.text

    async def test_no_messages_logged_by_default(self, messages, caplog):
        """By default, raw message content is NOT logged (PII safety)."""
        inner = _make_inner()
        module = AuditModule({}, inner)

        with caplog.at_level(logging.DEBUG, logger="arcllm.modules.audit"):
            await module.invoke(messages)

        assert "You are helpful" not in caplog.text
        assert "Hello there" not in caplog.text

    async def test_no_response_logged_by_default(self, messages, caplog):
        """By default, raw response content is NOT logged (PII safety)."""
        inner = _make_inner()
        module = AuditModule({}, inner)

        with caplog.at_level(logging.DEBUG, logger="arcllm.modules.audit"):
            await module.invoke(messages)

        assert "Hello there!" not in caplog.text


# ---------------------------------------------------------------------------
# TestAuditContentLogging — opt-in full content
# ---------------------------------------------------------------------------


class TestAuditContentLogging:
    async def test_include_messages_logs_message_content(self, messages, caplog):
        inner = _make_inner()
        module = AuditModule({"include_messages": True}, inner)

        with caplog.at_level(logging.DEBUG, logger="arcllm.modules.audit"):
            await module.invoke(messages)

        assert "You are helpful" in caplog.text

    async def test_include_response_logs_response_content(self, messages, caplog):
        inner = _make_inner()
        module = AuditModule({"include_response": True}, inner)

        with caplog.at_level(logging.DEBUG, logger="arcllm.modules.audit"):
            await module.invoke(messages)

        assert "Hello there!" in caplog.text

    async def test_include_both(self, messages, caplog):
        inner = _make_inner()
        module = AuditModule(
            {"include_messages": True, "include_response": True}, inner
        )

        with caplog.at_level(logging.DEBUG, logger="arcllm.modules.audit"):
            await module.invoke(messages)

        assert "You are helpful" in caplog.text
        assert "Hello there!" in caplog.text


# ---------------------------------------------------------------------------
# TestAuditLogLevel
# ---------------------------------------------------------------------------


class TestAuditLogLevel:
    async def test_default_log_level_is_info(self, messages, caplog):
        inner = _make_inner()
        module = AuditModule({}, inner)

        with caplog.at_level(logging.INFO, logger="arcllm.modules.audit"):
            await module.invoke(messages)
        assert "Audit" in caplog.text

    async def test_custom_log_level(self, messages, caplog):
        inner = _make_inner()
        module = AuditModule({"log_level": "DEBUG"}, inner)

        with caplog.at_level(logging.INFO, logger="arcllm.modules.audit"):
            await module.invoke(messages)
        assert caplog.text == ""

        with caplog.at_level(logging.DEBUG, logger="arcllm.modules.audit"):
            await module.invoke(messages)
        assert "Audit" in caplog.text

    def test_invalid_log_level_rejected(self):
        inner = _make_inner()
        with pytest.raises(ArcLLMConfigError, match="Invalid log_level"):
            AuditModule({"log_level": "NOPE"}, inner)


# ---------------------------------------------------------------------------
# TestAuditProviderInfo
# ---------------------------------------------------------------------------


class TestAuditProviderInfo:
    async def test_provider_name_from_inner(self):
        inner = _make_inner("my-provider")
        module = AuditModule({}, inner)
        assert module.name == "my-provider"

    async def test_model_name_from_inner(self):
        inner = _make_inner()
        module = AuditModule({}, inner)
        assert module.model_name == "test-model"


# ---------------------------------------------------------------------------
# TestAuditEdgeCases — review fixes
# ---------------------------------------------------------------------------


class TestAuditEdgeCases:
    async def test_exception_propagates_from_inner(self, messages):
        """Exceptions from inner.invoke() propagate without audit swallowing them."""
        inner = _make_inner()
        inner.invoke = AsyncMock(side_effect=RuntimeError("provider failure"))
        module = AuditModule({}, inner)

        with pytest.raises(RuntimeError, match="provider failure"):
            await module.invoke(messages)

    async def test_empty_tools_list_logs_zero(self, messages, tools, caplog):
        """tools=[] (empty list) logs tools_provided=0."""
        inner = _make_inner()
        module = AuditModule({}, inner)

        with caplog.at_level(logging.INFO, logger="arcllm.modules.audit"):
            await module.invoke(messages, tools=[])

        assert "tools_provided=0" in caplog.text

    async def test_empty_tool_calls_list_omitted(self, messages, caplog):
        """response.tool_calls=[] (empty list) omits tool_calls field."""
        inner = _make_inner()
        response = LLMResponse(
            content="Done",
            tool_calls=[],
            usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
            model="test-model",
            stop_reason="end_turn",
        )
        inner.invoke = AsyncMock(return_value=response)
        module = AuditModule({}, inner)

        with caplog.at_level(logging.INFO, logger="arcllm.modules.audit"):
            await module.invoke(messages)

        assert "tool_calls" not in caplog.text

    def test_unknown_config_key_rejected(self):
        """Typo'd config keys are caught at construction."""
        inner = _make_inner()
        with pytest.raises(ArcLLMConfigError, match="Unknown AuditModule config keys"):
            AuditModule({"include_mesages": True}, inner)

    async def test_model_name_with_newline_sanitized(self, messages, caplog):
        """Model names with control characters are sanitized in log output."""
        inner = _make_inner()
        response = LLMResponse(
            content="ok",
            usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
            model="gpt-4\nINJECTED",
            stop_reason="end_turn",
        )
        inner.invoke = AsyncMock(return_value=response)
        module = AuditModule({}, inner)

        with caplog.at_level(logging.INFO, logger="arcllm.modules.audit"):
            await module.invoke(messages)

        assert "INJECTED" not in caplog.text or "\\n" in caplog.text

    async def test_include_response_with_none_content(self, messages, caplog):
        """include_response=True handles None content without crashing."""
        inner = _make_inner()
        inner.invoke = AsyncMock(return_value=_TOOL_RESPONSE)
        module = AuditModule({"include_response": True}, inner)

        with caplog.at_level(logging.DEBUG, logger="arcllm.modules.audit"):
            await module.invoke(messages)

        assert "Audit response" in caplog.text

    async def test_content_logging_sanitizes_newlines(self, messages, caplog):
        """Opt-in content logging sanitizes control characters."""
        inner = _make_inner()
        response = LLMResponse(
            content="line1\nline2\rline3",
            usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
            model="test-model",
            stop_reason="end_turn",
        )
        inner.invoke = AsyncMock(return_value=response)
        module = AuditModule({"include_response": True}, inner)

        with caplog.at_level(logging.DEBUG, logger="arcllm.modules.audit"):
            await module.invoke(messages)

        assert "\\n" in caplog.text
        assert "\\r" in caplog.text
