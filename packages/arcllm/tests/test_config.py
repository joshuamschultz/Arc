"""Tests for ArcLLM config loading."""

from unittest.mock import patch

import pytest

from arcllm import ArcLLMConfigError, ArcLLMError
from arcllm.config import (
    DefaultsConfig,
    GlobalConfig,
    ModelMetadata,
    ModuleConfig,
    ProviderConfig,
    ProviderSettings,
    _validate_provider_name,
    load_global_config,
    load_provider_config,
)


# --- Global config loading ---


def test_load_global_config():
    config = load_global_config()
    assert isinstance(config, GlobalConfig)
    assert config.defaults.provider == "anthropic"
    assert config.defaults.temperature == 0.7
    assert config.defaults.max_tokens == 4096


def test_global_config_modules_all_disabled():
    config = load_global_config()
    for name, module in config.modules.items():
        assert module.enabled is False, f"Module {name} should be disabled by default"


def test_global_config_module_extra_fields():
    config = load_global_config()
    assert config.modules["budget"].monthly_limit_usd == 500.00
    assert config.modules["retry"].max_retries == 3
    assert config.modules["retry"].backoff_base_seconds == 1.0
    assert config.modules["fallback"].chain == ["anthropic", "openai"]
    assert config.modules["rate_limit"].requests_per_minute == 60


# --- Provider config loading ---


def test_load_provider_config_anthropic():
    config = load_provider_config("anthropic")
    assert isinstance(config, ProviderConfig)
    assert config.provider.api_format == "anthropic-messages"
    assert config.provider.base_url == "https://api.anthropic.com"
    assert config.provider.api_key_env == "ANTHROPIC_API_KEY"
    assert config.provider.default_model == "claude-sonnet-4-20250514"
    assert config.provider.default_temperature == 0.7


def test_provider_config_model_metadata():
    config = load_provider_config("anthropic")
    sonnet = config.models["claude-sonnet-4-20250514"]
    assert isinstance(sonnet, ModelMetadata)
    assert sonnet.context_window == 200000
    assert sonnet.max_output_tokens == 8192
    assert sonnet.supports_tools is True
    assert sonnet.supports_vision is True
    assert sonnet.supports_thinking is True
    assert sonnet.input_modalities == ["text", "image"]
    assert sonnet.cost_input_per_1m == 3.00
    assert sonnet.cost_output_per_1m == 15.00
    assert sonnet.cost_cache_read_per_1m == 0.30
    assert sonnet.cost_cache_write_per_1m == 3.75


def test_provider_config_multiple_models():
    config = load_provider_config("anthropic")
    assert len(config.models) == 2
    assert "claude-sonnet-4-20250514" in config.models
    assert "claude-haiku-4-5-20251001" in config.models


def test_load_provider_config_openai():
    config = load_provider_config("openai")
    assert config.provider.api_format == "openai-chat"
    assert config.provider.default_model == "gpt-4o"
    assert len(config.models) == 2
    assert "gpt-4o" in config.models
    assert "gpt-4o-mini" in config.models


# --- Error handling ---


def test_missing_provider_raises_config_error():
    with pytest.raises(ArcLLMConfigError, match="nonexistent"):
        load_provider_config("nonexistent")


def test_config_error_is_arcllm_error():
    err = ArcLLMConfigError("test error")
    assert isinstance(err, ArcLLMError)


# --- Type validation ---


def test_model_metadata_types():
    config = load_provider_config("anthropic")
    sonnet = config.models["claude-sonnet-4-20250514"]
    assert isinstance(sonnet.context_window, int)
    assert isinstance(sonnet.max_output_tokens, int)
    assert isinstance(sonnet.cost_input_per_1m, float)
    assert isinstance(sonnet.cost_output_per_1m, float)
    assert isinstance(sonnet.input_modalities, list)
    assert isinstance(sonnet.supports_tools, bool)


# --- Provider name validation (path traversal prevention) ---


def test_path_traversal_blocked():
    with pytest.raises(ArcLLMConfigError, match="Invalid provider name"):
        load_provider_config("../../etc/passwd")


def test_empty_provider_name_blocked():
    with pytest.raises(ArcLLMConfigError, match="cannot be empty"):
        load_provider_config("")


def test_uppercase_provider_name_blocked():
    with pytest.raises(ArcLLMConfigError, match="Invalid provider name"):
        load_provider_config("Anthropic")


def test_provider_name_too_long():
    with pytest.raises(ArcLLMConfigError, match="too long"):
        load_provider_config("a" * 65)


@pytest.mark.parametrize("name", ["../evil", "pro/vider", "a@b", "a b", ".hidden", "my-hyphen"])
def test_invalid_provider_names(name):
    with pytest.raises(ArcLLMConfigError):
        load_provider_config(name)


def test_valid_provider_name_format():
    _validate_provider_name("anthropic")
    _validate_provider_name("openai")
    _validate_provider_name("my_custom_provider")
    _validate_provider_name("o1")


# --- Error path coverage (FR-11, FR-12) ---


def test_malformed_toml_raises_config_error(tmp_path):
    bad_toml = tmp_path / "config.toml"
    bad_toml.write_text("[unclosed")

    with patch("arcllm.config._get_config_dir", return_value=tmp_path):
        with pytest.raises(ArcLLMConfigError, match="Failed to parse"):
            load_global_config()


def test_malformed_provider_toml_raises_config_error(tmp_path):
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    bad_toml = providers_dir / "badprovider.toml"
    bad_toml.write_text("not valid toml = = =")

    with patch("arcllm.config._get_config_dir", return_value=tmp_path):
        with pytest.raises(ArcLLMConfigError, match="Failed to parse"):
            load_provider_config("badprovider")


def test_invalid_types_in_global_config_raises_config_error(tmp_path):
    invalid_toml = tmp_path / "config.toml"
    invalid_toml.write_text('[defaults]\nprovider = "ok"\ntemperature = "not-a-float"\n')

    with patch("arcllm.config._get_config_dir", return_value=tmp_path):
        with pytest.raises(ArcLLMConfigError, match="Invalid global config"):
            load_global_config()


def test_invalid_types_in_provider_config_raises_config_error(tmp_path):
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    invalid_toml = providers_dir / "badtypes.toml"
    invalid_toml.write_text(
        '[provider]\napi_format = 123\nbase_url = true\n'
        'api_key_env = "OK"\ndefault_model = "OK"\ndefault_temperature = "nope"\n'
    )

    with patch("arcllm.config._get_config_dir", return_value=tmp_path):
        with pytest.raises(ArcLLMConfigError, match="Invalid provider config"):
            load_provider_config("badtypes")


def test_missing_global_config_raises_config_error(tmp_path):
    with patch("arcllm.config._get_config_dir", return_value=tmp_path):
        with pytest.raises(ArcLLMConfigError, match="not found"):
            load_global_config()


# --- HTTPS enforcement on base_url ---


def test_https_enforced_for_remote_hosts():
    with pytest.raises(Exception, match="HTTPS"):
        ProviderSettings(
            api_format="test",
            base_url="http://evil.example.com",
            api_key_env="TEST_KEY",
            default_model="m",
            default_temperature=0.7,
        )


def test_https_accepted():
    settings = ProviderSettings(
        api_format="test",
        base_url="https://api.example.com",
        api_key_env="TEST_KEY",
        default_model="m",
        default_temperature=0.7,
    )
    assert settings.base_url == "https://api.example.com"


def test_http_localhost_allowed():
    settings = ProviderSettings(
        api_format="test",
        base_url="http://localhost:8080",
        api_key_env="TEST_KEY",
        default_model="m",
        default_temperature=0.7,
    )
    assert settings.base_url == "http://localhost:8080"


def test_http_127_allowed():
    settings = ProviderSettings(
        api_format="test",
        base_url="http://127.0.0.1:11434",
        api_key_env="TEST_KEY",
        default_model="m",
        default_temperature=0.7,
    )
    assert settings.base_url == "http://127.0.0.1:11434"
