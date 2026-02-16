"""Tests for BaseModule — transparent wrapper foundation."""

from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest

from arcllm.modules.base import BaseModule
from arcllm.types import LLMProvider, LLMResponse, Message, Usage


@pytest.fixture
def mock_inner():
    """Create a mock LLMProvider for wrapping."""
    inner = MagicMock(spec=LLMProvider)
    inner.name = "test-provider"
    inner.model_name = "test-model"
    inner.validate_config.return_value = True
    inner.invoke = AsyncMock(
        return_value=LLMResponse(
            content="hello",
            usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
            model="test-model",
            stop_reason="end_turn",
        )
    )
    return inner


@pytest.fixture
def base_module(mock_inner):
    """Create a BaseModule wrapping a mock inner provider."""
    return BaseModule(config={}, inner=mock_inner)


class TestBaseModuleDelegation:
    async def test_delegates_invoke(self, base_module, mock_inner):
        messages = [Message(role="user", content="hi")]
        result = await base_module.invoke(messages)
        mock_inner.invoke.assert_awaited_once_with(messages, None)
        assert result.content == "hello"

    async def test_delegates_invoke_with_tools(self, base_module, mock_inner):
        messages = [Message(role="user", content="hi")]
        tools = [MagicMock()]
        await base_module.invoke(messages, tools=tools)
        mock_inner.invoke.assert_awaited_once_with(messages, tools)

    async def test_delegates_invoke_with_kwargs(self, base_module, mock_inner):
        messages = [Message(role="user", content="hi")]
        await base_module.invoke(messages, max_tokens=100)
        mock_inner.invoke.assert_awaited_once_with(messages, None, max_tokens=100)

    def test_delegates_name(self, base_module):
        assert base_module.name == "test-provider"

    def test_delegates_model_name(self, base_module):
        assert base_module.model_name == "test-model"

    def test_delegates_validate_config(self, base_module):
        assert base_module.validate_config() is True


class TestBaseModuleInterface:
    def test_is_llm_provider(self, base_module):
        assert isinstance(base_module, LLMProvider)

    async def test_response_unchanged(self, base_module):
        messages = [Message(role="user", content="hi")]
        result = await base_module.invoke(messages)
        assert isinstance(result, LLMResponse)
        assert result.content == "hello"
        assert result.stop_reason == "end_turn"
        assert result.usage.total_tokens == 15

    def test_stores_config(self, mock_inner):
        config = {"max_retries": 3}
        module = BaseModule(config=config, inner=mock_inner)
        assert module._config == config

    def test_stores_inner(self, base_module, mock_inner):
        assert base_module._inner is mock_inner


class TestBaseModuleClose:
    async def test_close_delegates_to_inner(self, mock_inner):
        mock_inner.close = AsyncMock()
        module = BaseModule(config={}, inner=mock_inner)
        await module.close()
        mock_inner.close.assert_awaited_once()

    async def test_close_skips_if_inner_has_no_close(self, mock_inner):
        # MagicMock(spec=LLMProvider) won't have close by default
        module = BaseModule(config={}, inner=mock_inner)
        # Should not raise
        await module.close()
