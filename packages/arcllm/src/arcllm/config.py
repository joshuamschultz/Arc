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
from urllib.parse import urlsplit

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
    # Claude 5-family models reject non-default sampling params with HTTP 400;
    # adapters omit temperature from the wire body when this is false.
    supports_temperature: bool = True
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
    if not v.startswith("http://"):
        return v

    host = (urlsplit(v).hostname or "").strip("[]")
    if host in ("localhost", "127.0.0.1", "::1"):
        return v

    # A single-label host — "litellm", "ollama", "proxy" — is a name that only
    # resolves inside a container network, a Kubernetes namespace, or a LAN. It
    # cannot be a public DNS name, so plain HTTP to it never leaves the private
    # network the caller is already inside, which is the risk this rule exists
    # to stop.
    #
    # Without this, a model gateway deployed as a sibling service was
    # unreachable: an agent pointed at `http://litellm:4000` failed config
    # validation, and the only ways out were to weaken the rule for every host
    # or to terminate TLS between two containers on the same bridge. Both are
    # worse than naming the case.
    #
    # A dotted host is still required to use HTTPS. "internal.example.com" is a
    # resolvable name and may route anywhere.
    if "." not in host and ":" not in host:
        return v

    raise ValueError(
        f"base_url must use HTTPS for remote hosts. Got: {v}. Plain HTTP is "
        f"allowed only for localhost and for single-label service names such "
        f"as http://litellm:4000, which cannot resolve outside a private network."
    )


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
    # "1h" (default) or "5m". A 1h write costs ~2x base vs ~1.25x for 5m, but an
    # Arc agent's turn cadence — scheduled runs, chat replies minutes apart — is
    # routinely longer than five minutes, so a 5m entry usually expires before it
    # is ever read. One extra read inside the hour already pays the difference
    # back. Set "5m" for a genuinely chatty deployment or a tighter exfil window.
    cache_ttl: str = "1h"

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


class ProviderKey(BaseModel):
    """Which environment variable one packaged provider reads its key from."""

    model_config = ConfigDict(frozen=True)

    provider: str
    api_key_env: str
    required: bool


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


def model_config_path() -> Path:
    """Return the canonical packaged model configuration file.

    Higher layers use this public coordinate instead of reaching into ArcLLM's
    private configuration helpers.  The location remains package-relative for
    compatibility with the existing configuration editor.
    """
    return _get_config_dir() / "config.toml"


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

    Layered like the global config: the packaged provider file is the base, and
    a ``[providers.<name>]`` table in ${ARC_CONFIG_DIR:-~/.arc}/arcllm.toml
    deep-merges over it. That is how a deployment points a provider at its own
    endpoint — the packaged default cannot know a private host, and editing an
    installed package to say so would put one machine's address in every
    machine's copy. The merged result is validated like any other, so an
    override cannot buy itself a rule the package would refuse.

    Raises ArcLLMConfigError on any failure.
    """
    _validate_provider_name(provider_name)
    config_path = _get_config_dir() / "providers" / f"{provider_name}.toml"
    data = _load_toml_file(config_path, f"provider config '{provider_name}'")

    user_path = _user_config_path()
    if user_path is not None:
        user_data = _load_toml_file(user_path, f"user config ({user_path})")
        overrides = user_data.get("providers", {})
        if isinstance(overrides, dict) and provider_name in overrides:
            data = _deep_merge(data, overrides[provider_name])

    try:
        provider_settings = ProviderSettings(**data.get("provider", {}))
        models = {
            name: ModelMetadata(**metadata) for name, metadata in data.get("models", {}).items()
        }
        endpoints = [EndpointConfig(**entry) for entry in data.get("endpoints", [])]
        return ProviderConfig(provider=provider_settings, models=models, endpoints=endpoints)
    except ValidationError as e:
        raise ArcLLMConfigError(f"Invalid provider config for '{provider_name}': {e}") from e


def list_provider_keys() -> tuple[ProviderKey, ...]:
    """Report the key coordinate of every packaged provider, ordered by name.

    The one reader of ``api_key_env`` for surfaces that ask "which variable does
    this provider need" (SPEC-064 D-581). Sourced by globbing the packaged
    ``providers/`` directory through :func:`load_provider_config`, so a new
    provider TOML is answerable the moment it ships and there is no second parser
    — nor a second, drifting copy of the map — anywhere in the stack.
    """
    directory = _get_config_dir() / "providers"
    keys: list[ProviderKey] = []
    for path in sorted(directory.glob("*.toml")):
        settings = load_provider_config(path.stem).provider
        keys.append(
            ProviderKey(
                provider=path.stem,
                api_key_env=settings.api_key_env,
                required=settings.api_key_required,
            )
        )
    return tuple(keys)


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
