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

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

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


def _enforce_https_for_remote(v: str) -> str:
    """Reject non-HTTPS ``base_url`` for remote hosts; allow HTTP for localhost.

    Shared by ``ProviderSettings`` and ``EndpointConfig`` (SPEC-017 FR-2) so a
    load-balanced pool endpoint gets exactly the same connection-security
    validation as the primary provider connection — one audited HTTPS rule,
    not two.
    """
    if v.startswith("http://") and not any(
        v.startswith(f"http://{host}") for host in ("localhost", "127.0.0.1", "[::1]")
    ):
        raise ValueError(f"base_url must use HTTPS for remote hosts. Got: {v}")
    return v


class ProviderSettings(BaseModel):
    """Provider connection settings from [provider] section."""

    api_format: str
    base_url: str
    api_key_env: str
    api_key_required: bool = True
    default_model: str
    default_temperature: float
    vault_path: str = ""
    # Provider prompt caching. Only adapters that support explicit cache
    # breakpoints (Anthropic) read these; OpenAI-wire adapters ignore them.
    # Default on: caching is a pure cost/latency win on a stable prefix.
    enable_prompt_caching: bool = True
    # "5m" (default, cheaper writes, smaller exfil window) or "1h" (opt-in for
    # long-lived agents whose turn cadence exceeds the 5-minute TTL).
    cache_ttl: str = "5m"

    @field_validator("cache_ttl")
    @classmethod
    def _validate_cache_ttl(cls, v: str) -> str:
        if v not in ("5m", "1h"):
            raise ValueError(f"cache_ttl must be '5m' or '1h'. Got: {v}")
        return v

    @field_validator("base_url")
    @classmethod
    def _validate_https(cls, v: str) -> str:
        return _enforce_https_for_remote(v)


class EndpointConfig(BaseModel):
    """One endpoint in a load-balanced pool (SPEC-017 [[endpoints]]).

    Mirrors the connection-relevant subset of ``ProviderSettings``: a pool
    endpoint is a variant *base_url*/key of the same provider, so it reuses
    the identical HTTPS validator and key-resolution contract (D-457) —
    there is no second, less-audited path for endpoint credentials.
    """

    base_url: str
    api_key_env: str = ""
    vault_path: str = ""
    weight: int = 1

    @field_validator("base_url")
    @classmethod
    def _validate_https(cls, v: str) -> str:
        return _enforce_https_for_remote(v)

    @field_validator("weight")
    @classmethod
    def _validate_weight(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"weight must be >= 0. Got: {v}")
        return v


class ProviderConfig(BaseModel):
    """Loaded provider TOML — connection settings + model metadata + endpoint pool."""

    provider: ProviderSettings
    models: dict[str, ModelMetadata]
    # Optional load-balancing pool (SPEC-017). Empty = single-endpoint,
    # today's behavior, byte-identical (FR-15 / SC-2).
    endpoints: list[EndpointConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_endpoint_key_sources(self) -> "ProviderConfig":
        """Every pool endpoint must resolve a key when the provider requires one.

        Cross-field check (needs ``provider.api_key_required``), so it lives
        here rather than on ``EndpointConfig`` itself, which has no
        visibility into the sibling ``[provider]`` section.
        """
        if not self.provider.api_key_required:
            return self
        for i, endpoint in enumerate(self.endpoints):
            if not endpoint.api_key_env and not endpoint.vault_path:
                raise ValueError(
                    f"endpoints[{i}] (base_url={endpoint.base_url!r}) requires "
                    "api_key_env or vault_path when api_key_required=true"
                )
        return self


class DefaultsConfig(BaseModel):
    """Global defaults from [defaults] section."""

    provider: str = "anthropic"
    temperature: float = 0.7
    max_tokens: int = 4096


class ModuleConfig(BaseModel):
    """Module toggle config. Extra fields preserved for module-specific settings."""

    model_config = ConfigDict(extra="allow")

    enabled: bool = False


class TraceEncryptionConfig(BaseModel):
    """Envelope-encryption settings for trace bodies at rest (SPEC-016 D-438).

    Disabled by default (personal/enterprise). Federal deployments set
    ``enabled=True`` and typically ``require_fips=True`` so construction
    fails closed unless the loaded crypto provider is FIPS-140-3-approved
    (SC-13). The wrapping key itself is resolved via the existing
    ``VaultResolver`` (D-447) — this model carries only *where* to look,
    never the key material.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    backend: str = ""
    key_ref: str = ""
    key_env: str = "ARCLLM_TRACE_WRAP_KEY"
    cache_ttl_seconds: int = 300
    require_fips: bool = False


class TraceRetentionConfig(BaseModel):
    """Retention purge bounds for rotated trace files (SPEC-016 D-440).

    ``None`` means unlimited for that dimension. Retention operates on
    whole rotated files, never on today's live chain — see
    ``arcllm.trace_retention.purge``.
    """

    model_config = ConfigDict(extra="forbid")

    max_age_days: int | None = None
    max_bytes: int | None = None


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

    Path: ``${ARC_CONFIG_DIR:-~/.arc}/arcllm.toml``.
    """
    base = os.environ.get("ARC_CONFIG_DIR")
    root = Path(base).expanduser() if base else Path.home() / ".arc"
    arcllm_toml = root / "arcllm.toml"
    return arcllm_toml if arcllm_toml.exists() else None


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

    Returns a typed ProviderConfig with connection settings, model metadata,
    and an optional load-balancing endpoint pool (SPEC-017 [[endpoints]]).
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
        endpoints = [EndpointConfig(**entry) for entry in data.get("endpoints", [])]
        return ProviderConfig(provider=provider_settings, models=models, endpoints=endpoints)
    except ValidationError as e:
        raise ArcLLMConfigError(f"Invalid provider config for '{provider_name}': {e}") from e


def load_telemetry_retention_config() -> TraceRetentionConfig:
    """Load ``[modules.telemetry.retention]`` as a typed, validated config.

    Retention purge operates on a ``JSONLTraceStore``'s directory, which is
    constructed by the caller (the store owns ``agent_root``, not the
    registry) — so this is a standalone accessor rather than something
    threaded automatically through ``load_model()``. Callers wire the
    result into ``JSONLTraceStore(agent_root, retention_max_age_days=...,
    retention_max_bytes=...)`` themselves (SPEC-016 FR-11).

    Raises:
        ArcLLMConfigError: On invalid retention settings.
    """
    global_config = load_global_config()
    telemetry_module = global_config.modules.get("telemetry")
    retention_data = getattr(telemetry_module, "retention", {}) if telemetry_module else {}
    try:
        return TraceRetentionConfig(**(retention_data or {}))
    except ValidationError as e:
        raise ArcLLMConfigError(f"Invalid telemetry retention config: {e}") from e
