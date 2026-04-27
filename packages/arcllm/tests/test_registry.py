"""Tests for ArcLLM provider registry and load_model()."""

import types as stdlib_types
from unittest.mock import patch

import pytest

from arcllm.config import load_provider_config as _real_load_provider_config
from arcllm.exceptions import ArcLLMConfigError
from arcllm.types import LLMProvider

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _set_api_keys(monkeypatch):
    """Set fake API keys for adapter construction."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")


@pytest.fixture(autouse=True)
def _clear_cache():
    """Clear the registry cache before and after each test."""
    from arcllm.registry import clear_cache

    clear_cache()
    yield
    clear_cache()


# ---------------------------------------------------------------------------
# TestLoadModelHappyPath
# ---------------------------------------------------------------------------


class TestLoadModelHappyPath:
    def test_load_anthropic_adapter(self):
        from arcllm.adapters.anthropic import AnthropicAdapter
        from arcllm.registry import load_model

        # security=False: test adapter construction without wrappers.
        # Per ADR-019, security is enabled by default; disable explicitly here.
        model = load_model("anthropic", telemetry=False, retry=False, queue=False, security=False)
        assert isinstance(model, AnthropicAdapter)

    def test_load_openai_adapter(self):
        from arcllm.adapters.openai import OpenaiAdapter
        from arcllm.registry import load_model

        # security=False: test adapter construction without wrappers.
        # Per ADR-019, security is enabled by default; disable explicitly here.
        model = load_model("openai", telemetry=False, retry=False, queue=False, security=False)
        assert isinstance(model, OpenaiAdapter)

    def test_load_default_model(self):
        from arcllm.registry import load_model

        model = load_model("anthropic")
        # default_model from anthropic.toml is claude-sonnet-4-6
        assert model.model_name == "claude-sonnet-4-6"

    def test_load_explicit_model(self):
        from arcllm.registry import load_model

        model = load_model("anthropic", "claude-haiku-4-5-20251001")
        assert model.model_name == "claude-haiku-4-5-20251001"

    def test_returns_llm_provider(self):
        from arcllm.registry import load_model

        model = load_model("anthropic")
        assert isinstance(model, LLMProvider)

    def test_nonexistent_model_accepted(self):
        """Unknown model name is allowed — adapter constructed with model_meta=None."""
        from arcllm.registry import load_model

        # security=False: test raw adapter without default security wrapper.
        # Per ADR-019, security is enabled by default; disable here to test
        # that the adapter accepts an unknown model name without rejection.
        model = load_model("anthropic", "claude-nonexistent-99", telemetry=False, retry=False, queue=False, security=False)
        assert model.model_name == "claude-nonexistent-99"
        assert model._model_meta is None

    def test_same_provider_different_models_returns_distinct_instances(self):
        """Cache stores config, not adapter instances. Each call returns a fresh adapter."""
        from arcllm.registry import load_model

        m1 = load_model("anthropic", "claude-sonnet-4-20250514")
        m2 = load_model("anthropic", "claude-haiku-4-5-20251001")
        assert m1 is not m2
        assert m1.model_name != m2.model_name


# ---------------------------------------------------------------------------
# TestConfigCaching
# ---------------------------------------------------------------------------


class TestConfigCaching:
    def test_config_cached(self):
        from arcllm.registry import load_model

        with patch(
            "arcllm.registry.load_provider_config",
            wraps=_real_load_provider_config,
        ) as mock_load:
            load_model("anthropic")
            load_model("anthropic")
            # Should only load config once — second call uses cache
            assert mock_load.call_count == 1

    def test_clear_cache_resets(self):
        from arcllm.registry import clear_cache, load_model

        with patch(
            "arcllm.registry.load_provider_config",
            wraps=_real_load_provider_config,
        ) as mock_load:
            load_model("anthropic")
            assert mock_load.call_count == 1

            clear_cache()
            load_model("anthropic")
            assert mock_load.call_count == 2

    def test_different_providers_cached_separately(self):
        from arcllm.registry import load_model

        with patch(
            "arcllm.registry.load_provider_config",
            wraps=_real_load_provider_config,
        ) as mock_load:
            load_model("anthropic")
            load_model("openai")
            load_model("anthropic")
            load_model("openai")
            # Each provider loaded once
            assert mock_load.call_count == 2

    def test_adapter_class_cached(self):
        """Adapter class is cached — importlib only called once per provider."""
        from arcllm.registry import _adapter_class_cache, load_model

        load_model("anthropic")
        assert "anthropic" in _adapter_class_cache

        load_model("anthropic")
        # Still just one entry — class was reused from cache
        assert len(_adapter_class_cache) == 1


# ---------------------------------------------------------------------------
# TestErrorHandling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_missing_provider_toml(self):
        from arcllm.registry import load_model

        with pytest.raises(ArcLLMConfigError, match="nonexistent"):
            load_model("nonexistent")

    def test_missing_adapter_module(self):
        from arcllm.registry import load_model

        # Patch load_provider_config to succeed, but module won't exist
        with patch("arcllm.registry.load_provider_config") as mock_config:
            mock_config.return_value = type(
                "FakeConfig",
                (),
                {"provider": type("FakeProvider", (), {"default_model": "test"})()},
            )()
            with pytest.raises(ArcLLMConfigError, match="adapter module"):
                load_model("nosuchadapter")

    def test_missing_adapter_class(self):
        from arcllm.registry import _get_adapter_class

        with patch("importlib.import_module") as mock_import:
            fake_module = stdlib_types.ModuleType("arcllm.adapters.fakeprov")
            mock_import.return_value = fake_module
            with pytest.raises(ArcLLMConfigError, match="FakeprovAdapter"):
                _get_adapter_class("fakeprov")

    def test_invalid_provider_name(self):
        from arcllm.registry import load_model

        with pytest.raises(ArcLLMConfigError, match="Invalid provider name"):
            load_model("../etc/passwd")

    def test_empty_provider_name(self):
        from arcllm.registry import load_model

        with pytest.raises(ArcLLMConfigError, match="cannot be empty"):
            load_model("")

    def test_uppercase_provider_name(self):
        from arcllm.registry import load_model

        with pytest.raises(ArcLLMConfigError, match="Invalid provider name"):
            load_model("ANTHROPIC")

    def test_broken_adapter_module_caught(self):
        """ImportError from a broken adapter (not just missing) is caught cleanly."""
        from arcllm.registry import _get_adapter_class

        with patch("importlib.import_module", side_effect=ImportError("bad dependency")):
            with pytest.raises(ArcLLMConfigError, match="adapter module"):
                _get_adapter_class("brokenprovider")


# ---------------------------------------------------------------------------
# TestModuleStacking
# ---------------------------------------------------------------------------


class TestModuleStacking:
    """Registry integration: load_model() wraps adapters with modules."""

    def test_load_model_with_retry_kwarg(self):
        """retry=True wraps adapter with RetryModule.

        Per ADR-019, security is enabled by default (outermost after retry).
        Disable security so retry is the observable outermost wrapper.
        """
        from arcllm.modules.retry import RetryModule
        from arcllm.registry import load_model

        model = load_model("anthropic", retry=True, telemetry=False, queue=False, security=False)
        assert isinstance(model, RetryModule)

    def test_load_model_with_retry_dict(self):
        """retry={...} wraps adapter with RetryModule using custom config.

        Per ADR-019, security is enabled by default (outermost after retry).
        Disable security so retry is the observable outermost wrapper.
        """
        from arcllm.modules.retry import RetryModule
        from arcllm.registry import load_model

        model = load_model("anthropic", retry={"max_retries": 5}, telemetry=False, queue=False, security=False)
        assert isinstance(model, RetryModule)
        assert model._max_retries == 5

    def test_load_model_with_config_retry(self):
        """Config.toml retry.enabled=true wraps adapter with RetryModule."""
        from arcllm.config import GlobalConfig, ModuleConfig
        from arcllm.modules.retry import RetryModule
        from arcllm.registry import load_model

        mock_global = GlobalConfig(
            defaults={"provider": "anthropic", "temperature": 0.7, "max_tokens": 4096},
            modules={"retry": ModuleConfig(enabled=True, max_retries=2)},
        )
        with patch("arcllm.registry.load_global_config", return_value=mock_global):
            model = load_model("anthropic")
        assert isinstance(model, RetryModule)

    def test_load_model_retry_false_overrides_config(self):
        """retry=False disables retry even if config.toml enables it."""
        from arcllm.adapters.anthropic import AnthropicAdapter
        from arcllm.config import GlobalConfig, ModuleConfig
        from arcllm.modules.retry import RetryModule
        from arcllm.registry import load_model

        mock_global = GlobalConfig(
            defaults={"provider": "anthropic", "temperature": 0.7, "max_tokens": 4096},
            modules={"retry": ModuleConfig(enabled=True, max_retries=2)},
        )
        with patch("arcllm.registry.load_global_config", return_value=mock_global):
            model = load_model("anthropic", retry=False)
        assert not isinstance(model, RetryModule)
        assert isinstance(model, AnthropicAdapter)

    def test_load_model_with_fallback(self):
        """fallback=True wraps adapter with FallbackModule.

        Per ADR-019, security is enabled by default (outermost after fallback).
        Disable security so fallback is the observable outermost wrapper.
        """
        from arcllm.modules.fallback import FallbackModule
        from arcllm.registry import load_model

        model = load_model("anthropic", fallback=True, telemetry=False, retry=False, queue=False, security=False)
        assert isinstance(model, FallbackModule)

    def test_load_model_with_fallback_dict(self):
        """fallback={...} wraps adapter with FallbackModule using custom config.

        Per ADR-019, security is enabled by default (outermost after fallback).
        Disable security so fallback is the observable outermost wrapper.
        """
        from arcllm.modules.fallback import FallbackModule
        from arcllm.registry import load_model

        model = load_model("anthropic", fallback={"chain": ["openai"]}, telemetry=False, retry=False, queue=False, security=False)
        assert isinstance(model, FallbackModule)
        assert model._chain == ["openai"]

    def test_load_model_retry_and_fallback_stacking_order(self):
        """Stacking order: Security(Retry(Fallback(adapter))).

        Per ADR-019, security wraps outermost over retry+fallback.
        Disable security to assert pure retry/fallback order.
        """
        from arcllm.adapters.anthropic import AnthropicAdapter
        from arcllm.modules.fallback import FallbackModule
        from arcllm.modules.retry import RetryModule
        from arcllm.registry import load_model

        model = load_model("anthropic", retry=True, fallback=True, telemetry=False, queue=False, security=False)
        # Outermost is Retry (security disabled)
        assert isinstance(model, RetryModule)
        # Inner is Fallback
        assert isinstance(model._inner, FallbackModule)
        # Innermost is the adapter
        assert isinstance(model._inner._inner, AnthropicAdapter)

    def test_load_model_no_modules_unchanged(self):
        """With all modules disabled, adapter returned directly.

        Per ADR-019, security is enabled by default. Disable all modules
        explicitly to assert a bare adapter is returned.
        """
        from arcllm.adapters.anthropic import AnthropicAdapter
        from arcllm.registry import load_model

        model = load_model("anthropic", telemetry=False, retry=False, queue=False, security=False)
        assert isinstance(model, AnthropicAdapter)

    def test_load_model_retry_kwarg_overrides_config_values(self):
        """retry={max_retries: 10} overrides config.toml max_retries=2."""
        from arcllm.config import GlobalConfig, ModuleConfig
        from arcllm.modules.retry import RetryModule
        from arcllm.registry import load_model

        mock_global = GlobalConfig(
            defaults={"provider": "anthropic", "temperature": 0.7, "max_tokens": 4096},
            modules={"retry": ModuleConfig(enabled=True, max_retries=2)},
        )
        with patch("arcllm.registry.load_global_config", return_value=mock_global):
            model = load_model("anthropic", retry={"max_retries": 10})
        assert isinstance(model, RetryModule)
        assert model._max_retries == 10

    def test_load_model_with_rate_limit(self):
        """rate_limit=True wraps adapter with RateLimitModule.

        Per ADR-019, security is enabled by default (outermost after rate_limit).
        Disable security so rate_limit is the observable outermost wrapper.
        """
        from arcllm.modules.rate_limit import RateLimitModule
        from arcllm.registry import load_model

        model = load_model("anthropic", rate_limit=True, telemetry=False, retry=False, queue=False, security=False)
        assert isinstance(model, RateLimitModule)

    def test_load_model_with_rate_limit_dict(self):
        """rate_limit={...} wraps adapter with custom RPM.

        Per ADR-019, security is enabled by default (outermost after rate_limit).
        Disable security so rate_limit is the observable outermost wrapper.
        """
        from arcllm.modules.rate_limit import RateLimitModule
        from arcllm.registry import load_model

        model = load_model("anthropic", rate_limit={"requests_per_minute": 120}, telemetry=False, retry=False, queue=False, security=False)
        assert isinstance(model, RateLimitModule)

    def test_load_model_rate_limit_false_overrides_config(self):
        """rate_limit=False disables even if config.toml enables it."""
        from arcllm.adapters.anthropic import AnthropicAdapter
        from arcllm.config import GlobalConfig, ModuleConfig
        from arcllm.modules.rate_limit import RateLimitModule
        from arcllm.registry import load_model

        mock_global = GlobalConfig(
            defaults={"provider": "anthropic", "temperature": 0.7, "max_tokens": 4096},
            modules={"rate_limit": ModuleConfig(enabled=True, requests_per_minute=60)},
        )
        with patch("arcllm.registry.load_global_config", return_value=mock_global):
            model = load_model("anthropic", rate_limit=False)
        assert not isinstance(model, RateLimitModule)
        assert isinstance(model, AnthropicAdapter)

    def test_load_model_full_stack_order_without_telemetry(self):
        """Stacking order without telemetry or security: Retry(Fallback(RateLimit(adapter))).

        Per ADR-019, security is enabled by default. Disable security explicitly
        to test the pure retry/fallback/rate_limit stacking order.
        """
        from arcllm.adapters.anthropic import AnthropicAdapter
        from arcllm.modules.fallback import FallbackModule
        from arcllm.modules.rate_limit import RateLimitModule
        from arcllm.modules.retry import RetryModule
        from arcllm.registry import load_model

        model = load_model("anthropic", retry=True, fallback=True, rate_limit=True, telemetry=False, queue=False, security=False)
        assert isinstance(model, RetryModule)
        assert isinstance(model._inner, FallbackModule)
        assert isinstance(model._inner._inner, RateLimitModule)
        assert isinstance(model._inner._inner._inner, AnthropicAdapter)

    def test_load_model_with_telemetry(self):
        """telemetry=True wraps adapter with TelemetryModule."""
        from arcllm.modules.telemetry import TelemetryModule
        from arcllm.registry import load_model

        model = load_model("anthropic", telemetry=True, retry=False, queue=False)
        assert isinstance(model, TelemetryModule)

    def test_load_model_telemetry_injects_pricing(self):
        """TelemetryModule receives cost rates from provider model metadata."""
        from arcllm.modules.telemetry import TelemetryModule
        from arcllm.registry import load_model

        model = load_model("anthropic", telemetry=True, retry=False, queue=False)
        assert isinstance(model, TelemetryModule)
        assert model._cost_input == 3.00
        assert model._cost_output == 15.00
        assert model._cost_cache_read == 0.30
        assert model._cost_cache_write == 3.75

    def test_load_model_telemetry_custom_model_pricing(self):
        """Pricing comes from the specific model requested."""
        from arcllm.modules.telemetry import TelemetryModule
        from arcllm.registry import load_model

        model = load_model("anthropic", "claude-haiku-4-5-20251001", telemetry=True, retry=False, queue=False)
        assert isinstance(model, TelemetryModule)
        assert model._cost_input == 0.80
        assert model._cost_output == 4.00

    def test_load_model_telemetry_dict_overrides_pricing(self):
        """Explicit cost in kwarg dict overrides model metadata pricing."""
        from arcllm.modules.telemetry import TelemetryModule
        from arcllm.registry import load_model

        model = load_model("anthropic", telemetry={"cost_input_per_1m": 99.0}, retry=False, queue=False)
        assert isinstance(model, TelemetryModule)
        # Explicit override wins
        assert model._cost_input == 99.0
        # Other costs still injected from metadata
        assert model._cost_output == 15.00

    def test_load_model_full_stack_with_telemetry(self):
        """Stacking order: Telemetry(Security(Retry(Fallback(RateLimit(adapter))))).

        Per ADR-019, security is enabled by default — it sits between
        Audit and Retry in the stack. Disable security to assert the
        pure telemetry/retry/fallback/rate_limit order.
        """
        from arcllm.adapters.anthropic import AnthropicAdapter
        from arcllm.modules.fallback import FallbackModule
        from arcllm.modules.rate_limit import RateLimitModule
        from arcllm.modules.retry import RetryModule
        from arcllm.modules.telemetry import TelemetryModule
        from arcllm.registry import load_model

        model = load_model(
            "anthropic",
            retry=True,
            fallback=True,
            rate_limit=True,
            telemetry=True,
            queue=False,
            security=False,
        )
        assert isinstance(model, TelemetryModule)
        assert isinstance(model._inner, RetryModule)
        assert isinstance(model._inner._inner, FallbackModule)
        assert isinstance(model._inner._inner._inner, RateLimitModule)
        assert isinstance(model._inner._inner._inner._inner, AnthropicAdapter)

    def test_load_model_telemetry_false_overrides_config(self):
        """telemetry=False disables even if config.toml enables it."""
        from arcllm.adapters.anthropic import AnthropicAdapter
        from arcllm.config import GlobalConfig, ModuleConfig
        from arcllm.modules.telemetry import TelemetryModule
        from arcllm.registry import load_model

        mock_global = GlobalConfig(
            defaults={"provider": "anthropic", "temperature": 0.7, "max_tokens": 4096},
            modules={"telemetry": ModuleConfig(enabled=True)},
        )
        with patch("arcllm.registry.load_global_config", return_value=mock_global):
            model = load_model("anthropic", telemetry=False)
        assert not isinstance(model, TelemetryModule)
        assert isinstance(model, AnthropicAdapter)

    def test_load_model_with_audit(self):
        """audit=True wraps adapter with AuditModule."""
        from arcllm.modules.audit import AuditModule
        from arcllm.registry import load_model

        model = load_model("anthropic", audit=True, telemetry=False, retry=False, queue=False)
        assert isinstance(model, AuditModule)

    def test_load_model_audit_false_overrides_config(self):
        """audit=False disables even if config.toml enables it."""
        from arcllm.adapters.anthropic import AnthropicAdapter
        from arcllm.config import GlobalConfig, ModuleConfig
        from arcllm.modules.audit import AuditModule
        from arcllm.registry import load_model

        mock_global = GlobalConfig(
            defaults={"provider": "anthropic", "temperature": 0.7, "max_tokens": 4096},
            modules={"audit": ModuleConfig(enabled=True)},
        )
        with patch("arcllm.registry.load_global_config", return_value=mock_global):
            model = load_model("anthropic", audit=False)
        assert not isinstance(model, AuditModule)
        assert isinstance(model, AnthropicAdapter)

    def test_load_model_full_stack_with_audit(self):
        """Stacking order: Telemetry(Audit(Retry(Fallback(RateLimit(adapter))))).

        Per ADR-019, security is enabled by default (sits between Audit and Retry).
        Disable security to test the pure audit/retry/fallback/rate_limit order.
        """
        from arcllm.adapters.anthropic import AnthropicAdapter
        from arcllm.modules.audit import AuditModule
        from arcllm.modules.fallback import FallbackModule
        from arcllm.modules.rate_limit import RateLimitModule
        from arcllm.modules.retry import RetryModule
        from arcllm.modules.telemetry import TelemetryModule
        from arcllm.registry import load_model

        model = load_model(
            "anthropic",
            retry=True,
            fallback=True,
            rate_limit=True,
            telemetry=True,
            audit=True,
            queue=False,
            security=False,
        )
        assert isinstance(model, TelemetryModule)
        assert isinstance(model._inner, AuditModule)
        assert isinstance(model._inner._inner, RetryModule)
        assert isinstance(model._inner._inner._inner, FallbackModule)
        assert isinstance(model._inner._inner._inner._inner, RateLimitModule)
        assert isinstance(model._inner._inner._inner._inner._inner, AnthropicAdapter)

    def test_load_model_with_otel(self):
        """otel={exporter: none} wraps adapter with OtelModule."""
        from arcllm.modules.otel import OtelModule
        from arcllm.registry import load_model

        model = load_model("anthropic", otel={"exporter": "none"})
        assert isinstance(model, OtelModule)

    def test_load_model_with_queue(self):
        """queue=True wraps adapter with QueueModule."""
        from arcllm.modules.queue import QueueModule
        from arcllm.registry import load_model

        model = load_model("anthropic", queue=True)
        assert isinstance(model, QueueModule)

    def test_load_model_with_queue_dict(self):
        """queue={...} wraps adapter with QueueModule using custom config."""
        from arcllm.modules.queue import QueueModule
        from arcllm.registry import load_model

        model = load_model("anthropic", queue={"max_concurrent": 5})
        assert isinstance(model, QueueModule)
        assert model._max_concurrent == 5

    def test_load_model_queue_false_overrides_config(self):
        """queue=False disables even if config.toml enables it."""
        from arcllm.adapters.anthropic import AnthropicAdapter
        from arcllm.config import GlobalConfig, ModuleConfig
        from arcllm.modules.queue import QueueModule
        from arcllm.registry import load_model

        mock_global = GlobalConfig(
            defaults={"provider": "anthropic", "temperature": 0.7, "max_tokens": 4096},
            modules={"queue": ModuleConfig(enabled=True, max_concurrent=2)},
        )
        with patch("arcllm.registry.load_global_config", return_value=mock_global):
            model = load_model("anthropic", queue=False)
        assert not isinstance(model, QueueModule)
        assert isinstance(model, AnthropicAdapter)

    def test_load_model_otel_full_stack(self):
        """Full stack: Otel(Queue(Telemetry(Audit(Retry(Fallback(RateLimit(adapter))))))).

        Per ADR-019, security is enabled by default (sits between Audit and Retry).
        Disable security to test the complete stack order without the security wrapper.
        """
        from arcllm.adapters.anthropic import AnthropicAdapter
        from arcllm.modules.audit import AuditModule
        from arcllm.modules.fallback import FallbackModule
        from arcllm.modules.otel import OtelModule
        from arcllm.modules.queue import QueueModule
        from arcllm.modules.rate_limit import RateLimitModule
        from arcllm.modules.retry import RetryModule
        from arcllm.modules.telemetry import TelemetryModule
        from arcllm.registry import load_model

        model = load_model(
            "anthropic",
            retry=True,
            fallback=True,
            rate_limit=True,
            telemetry=True,
            queue=True,
            audit=True,
            security=False,
            otel={"exporter": "none"},
        )
        assert isinstance(model, OtelModule)
        assert isinstance(model._inner, QueueModule)
        assert isinstance(model._inner._inner, TelemetryModule)
        assert isinstance(model._inner._inner._inner, AuditModule)
        assert isinstance(model._inner._inner._inner._inner, RetryModule)
        assert isinstance(model._inner._inner._inner._inner._inner, FallbackModule)
        assert isinstance(model._inner._inner._inner._inner._inner._inner, RateLimitModule)
        assert isinstance(model._inner._inner._inner._inner._inner._inner._inner, AnthropicAdapter)

    def test_load_model_otel_false_overrides_config(self):
        """otel=False disables even if config.toml enables it."""
        from arcllm.adapters.anthropic import AnthropicAdapter
        from arcllm.config import GlobalConfig, ModuleConfig
        from arcllm.modules.otel import OtelModule
        from arcllm.registry import load_model

        mock_global = GlobalConfig(
            defaults={"provider": "anthropic", "temperature": 0.7, "max_tokens": 4096},
            modules={"otel": ModuleConfig(enabled=True, exporter="none")},
        )
        with patch("arcllm.registry.load_global_config", return_value=mock_global):
            model = load_model("anthropic", otel=False)
        assert not isinstance(model, OtelModule)
        assert isinstance(model, AnthropicAdapter)

    def test_load_model_otel_dict_overrides_config(self):
        """otel={...} kwarg dict merges over config.toml defaults."""
        from arcllm.config import GlobalConfig, ModuleConfig
        from arcllm.modules.otel import OtelModule
        from arcllm.registry import load_model

        mock_global = GlobalConfig(
            defaults={"provider": "anthropic", "temperature": 0.7, "max_tokens": 4096},
            modules={"otel": ModuleConfig(enabled=True, exporter="otlp")},
        )
        with patch("arcllm.registry.load_global_config", return_value=mock_global):
            # kwarg overrides config.toml exporter from "otlp" to "none"
            model = load_model("anthropic", otel={"exporter": "none"})
        assert isinstance(model, OtelModule)

    def test_load_model_otel_only(self):
        """OTel without other modules — just Otel(adapter).

        Per ADR-019, security is enabled by default. Disable security to
        test that Otel wraps the adapter directly with no other layers.
        """
        from arcllm.adapters.anthropic import AnthropicAdapter
        from arcllm.modules.otel import OtelModule
        from arcllm.registry import load_model

        model = load_model("anthropic", otel={"exporter": "none"}, telemetry=False, retry=False, queue=False, security=False)
        assert isinstance(model, OtelModule)
        assert isinstance(model._inner, AnthropicAdapter)

    def test_clear_cache_clears_buckets(self):
        """clear_cache() resets rate limit shared state."""
        from arcllm.modules.rate_limit import _bucket_registry
        from arcllm.registry import clear_cache, load_model

        load_model("anthropic", rate_limit=True)
        assert "anthropic" in _bucket_registry
        clear_cache()
        assert len(_bucket_registry) == 0
