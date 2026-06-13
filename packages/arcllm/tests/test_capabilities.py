"""Tests for arcllm.capabilities — model-tool-call discovery helpers."""

from __future__ import annotations

import pytest

import arcllm
from arcllm.capabilities import supports_tools, tool_capable_models


class TestSupportsTools:
    def test_known_tool_capable_model_returns_true(self) -> None:
        """anthropic ships at least one tool-capable model in its TOML."""
        capable = tool_capable_models("anthropic")
        assert capable, "anthropic provider should expose tool-capable models"
        assert supports_tools("anthropic", capable[0]) is True

    def test_unknown_provider_returns_false(self) -> None:
        assert supports_tools("not-a-real-provider", "anything") is False

    def test_unknown_model_returns_false(self) -> None:
        assert supports_tools("anthropic", "definitely-not-a-real-model") is False


class TestToolCapableModels:
    def test_returns_subset_of_provider_models(self) -> None:
        capable = tool_capable_models("anthropic")
        # All returned names must report supports_tools == True.
        for name in capable:
            assert supports_tools("anthropic", name) is True

    def test_unknown_provider_returns_empty(self) -> None:
        assert tool_capable_models("not-a-real-provider") == []


class TestArcllmReexport:
    def test_capabilities_helpers_exposed_at_top_level(self) -> None:
        # Callers should be able to ``from arcllm import supports_tools`` —
        # the helpers are intended to be discoverable from the package root.
        assert arcllm.supports_tools is supports_tools
        assert arcllm.tool_capable_models is tool_capable_models


class TestAdapterToolCheck:
    """``BaseAdapter._check_tool_capability`` converts the silent
    non-tool-capable failure into a loud ArcLLMConfigError at invoke."""

    def test_check_skipped_without_tools(self) -> None:
        from arcllm.adapters.openai import OpenaiAdapter
        from arcllm.config import (
            ModelMetadata,
            ProviderConfig,
            ProviderSettings,
        )

        provider = ProviderSettings(
            api_format="openai",
            base_url="https://example.com",
            api_key_env="X",
            api_key_required=False,
            default_model="m",
            default_temperature=0.5,
        )
        meta = ModelMetadata(
            context_window=128_000,
            max_output_tokens=4096,
            supports_tools=False,
            supports_vision=False,
            supports_thinking=False,
            input_modalities=["text"],
            cost_input_per_1m=0.0,
            cost_output_per_1m=0.0,
            cost_cache_read_per_1m=0.0,
            cost_cache_write_per_1m=0.0,
        )
        adapter = OpenaiAdapter(ProviderConfig(provider=provider, models={"m": meta}), "m")
        # No tools → check is a no-op.
        adapter._check_tool_capability(None)
        adapter._check_tool_capability([])

    def test_check_raises_when_tools_passed_to_non_tool_model(self) -> None:
        from arcllm.adapters.openai import OpenaiAdapter
        from arcllm.config import ModelMetadata, ProviderConfig, ProviderSettings
        from arcllm.exceptions import ArcLLMConfigError

        provider = ProviderSettings(
            api_format="openai",
            base_url="https://example.com",
            api_key_env="X",
            api_key_required=False,
            default_model="m",
            default_temperature=0.5,
        )
        meta = ModelMetadata(
            context_window=128_000,
            max_output_tokens=4096,
            supports_tools=False,
            supports_vision=False,
            supports_thinking=False,
            input_modalities=["text"],
            cost_input_per_1m=0.0,
            cost_output_per_1m=0.0,
            cost_cache_read_per_1m=0.0,
            cost_cache_write_per_1m=0.0,
        )
        adapter = OpenaiAdapter(ProviderConfig(provider=provider, models={"m": meta}), "m")
        with pytest.raises(ArcLLMConfigError, match="not marked tool-capable"):
            adapter._check_tool_capability([{"name": "search"}])

    def test_check_allows_models_without_declared_metadata(self) -> None:
        """A model with no TOML entry is allowed through — adapter can't
        prove it's non-capable, and blocking would be wrong for new models."""
        from arcllm.adapters.openai import OpenaiAdapter
        from arcllm.config import ProviderConfig, ProviderSettings

        provider = ProviderSettings(
            api_format="openai",
            base_url="https://example.com",
            api_key_env="X",
            api_key_required=False,
            default_model="brand-new",
            default_temperature=0.5,
        )
        adapter = OpenaiAdapter(ProviderConfig(provider=provider, models={}), "brand-new")
        # Should not raise.
        adapter._check_tool_capability([{"name": "search"}])
