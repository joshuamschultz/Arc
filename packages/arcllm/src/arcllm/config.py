"""ArcLLM config loading — TOML-based, validated on load.

Layered precedence (later wins):
  1. Packaged defaults at <arcllm>/config.toml
  2. User overrides at ${ARC_CONFIG_DIR:-~/.arc}/arcllm.toml

Dicts deep-merge; lists and scalars are replaced. Missing user file =
no-op (current behavior preserved).
"""

import os
import re
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from arcllm.exceptions import ArcLLMConfigError

_PROVIDER_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


# ---------------------------------------------------------------------------
# Config models
# ---------------------------------------------------------------------------


class ModelMetadata(BaseModel):
    """Per-model metadata from provider TOML [models.*] sections."""

    context_window: int
    max_output_tokens: int
    supports_tools: bool
    supports_vision: bool
    supports_thinking: bool
    input_modalities: list[str]
    cost_input_per_1m: float
    cost_output_per_1m: float
    cost_cache_read_per_1m: float
    cost_cache_write_per_1m: float


class ProviderSettings(BaseModel):
    """Provider connection settings from [provider] section."""

    api_format: str
    base_url: str
    api_key_env: str
    api_key_required: bool = True
    default_model: str
    default_temperature: float
    vault_path: str = ""

    @field_validator("base_url")
    @classmethod
    def _validate_https(cls, v: str) -> str:
        """Enforce HTTPS for remote hosts. Allow HTTP only for localhost."""
        if v.startswith("http://") and not any(
            v.startswith(f"http://{host}") for host in ("localhost", "127.0.0.1", "[::1]")
        ):
            raise ValueError(f"base_url must use HTTPS for remote hosts. Got: {v}")
        return v


class ProviderConfig(BaseModel):
    """Loaded provider TOML — connection settings + model metadata."""

    provider: ProviderSettings
    models: dict[str, ModelMetadata]


class DefaultsConfig(BaseModel):
    """Global defaults from [defaults] section."""

    provider: str = "anthropic"
    temperature: float = 0.7
    max_tokens: int = 4096


class ModuleConfig(BaseModel):
    """Module toggle config. Extra fields preserved for module-specific settings."""

    model_config = ConfigDict(extra="allow")

    enabled: bool = False


class VaultConfig(BaseModel):
    """Vault backend configuration from [vault] section."""

    backend: str = ""
    cache_ttl_seconds: int = 300
    url: str = ""
    region: str = ""


class GlobalConfig(BaseModel):
    """Loaded global config.toml — defaults + module toggles."""

    defaults: DefaultsConfig
    modules: dict[str, ModuleConfig]
    vault: VaultConfig = VaultConfig()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_config_dir() -> Path:
    """Return the directory containing packaged config files (package-relative)."""
    return Path(__file__).parent


def _user_config_path() -> Path | None:
    """Return the user-override config path, or None if absent.

    Preferred: ``${ARC_CONFIG_DIR:-~/.arc}/arcllm.toml``.
    Deprecated fallback: the legacy ``config.toml`` from earlier ``arc init``
    versions is honored if no ``arcllm.toml`` exists.
    """
    base = os.environ.get("ARC_CONFIG_DIR")
    root = Path(base).expanduser() if base else Path.home() / ".arc"
    preferred = root / "arcllm.toml"
    if preferred.exists():
        return preferred
    legacy = root / "config.toml"
    if legacy.exists():
        return legacy
    return None


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge override into base. Dicts merge; lists & scalars replace."""
    result = dict(base)
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def _load_toml_file(path: Path, context: str) -> dict[str, Any]:
    """Load and parse a TOML file with consistent error handling.

    Args:
        path: Absolute path to the TOML file.
        context: Human-readable label for error messages (e.g., "global config").

    Raises:
        ArcLLMConfigError: On missing file or malformed TOML.
    """
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError as e:
        raise ArcLLMConfigError(f"{context} not found: {path}") from e
    except tomllib.TOMLDecodeError as e:
        raise ArcLLMConfigError(f"Failed to parse {context}: {e}") from e


def _validate_provider_name(provider_name: str) -> None:
    """Validate provider name is safe for path construction.

    Prevents path traversal (NIST 800-53 AC-3) by restricting to
    lowercase alphanumeric + underscores, max 64 characters.

    Raises:
        ArcLLMConfigError: On invalid provider name.
    """
    if not provider_name:
        raise ArcLLMConfigError("Provider name cannot be empty")
    if len(provider_name) > 64:
        raise ArcLLMConfigError("Provider name too long (max 64 characters)")
    if not _PROVIDER_NAME_RE.match(provider_name):
        raise ArcLLMConfigError(
            f"Invalid provider name '{provider_name}'. "
            "Must start with a letter and contain only lowercase letters, "
            "numbers, and underscores."
        )


# ---------------------------------------------------------------------------
# Loader functions
# ---------------------------------------------------------------------------


def load_global_config() -> GlobalConfig:
    """Load and validate the global config.toml.

    Layered: packaged config.toml is the base; ${ARC_CONFIG_DIR:-~/.arc}/arcllm.toml
    deep-merges over it when present. Returns a typed GlobalConfig.
    Raises ArcLLMConfigError on any failure.
    """
    packaged_path = _get_config_dir() / "config.toml"
    data = _load_toml_file(packaged_path, "global config")

    user_path = _user_config_path()
    if user_path is not None:
        user_data = _load_toml_file(user_path, f"user config ({user_path})")
        data = _deep_merge(data, user_data)

    try:
        defaults = DefaultsConfig(**data.get("defaults", {}))
        modules = {
            name: ModuleConfig(**settings) for name, settings in data.get("modules", {}).items()
        }
        vault = VaultConfig(**data.get("vault", {}))
        return GlobalConfig(defaults=defaults, modules=modules, vault=vault)
    except ValidationError as e:
        raise ArcLLMConfigError(f"Invalid global config: {e}") from e


def load_provider_config(provider_name: str) -> ProviderConfig:
    """Load and validate a provider TOML file.

    Args:
        provider_name: Provider identifier (e.g., "anthropic", "openai").

    Returns a typed ProviderConfig with connection settings and model metadata.
    Raises ArcLLMConfigError on any failure.
    """
    _validate_provider_name(provider_name)
    config_path = _get_config_dir() / "providers" / f"{provider_name}.toml"
    data = _load_toml_file(config_path, f"provider config '{provider_name}'")

    try:
        provider_settings = ProviderSettings(**data.get("provider", {}))
        models = {
            name: ModelMetadata(**metadata) for name, metadata in data.get("models", {}).items()
        }
        return ProviderConfig(provider=provider_settings, models=models)
    except ValidationError as e:
        raise ArcLLMConfigError(f"Invalid provider config for '{provider_name}': {e}") from e
