"""Provider registry — convention-based adapter discovery and load_model()."""

import importlib
import os
from typing import Any

from arcllm.config import ProviderConfig, load_global_config, load_provider_config
from arcllm.exceptions import ArcLLMConfigError
from arcllm.types import LLMProvider

# Module-level caches: loaded once per provider, reused across calls.
# Thread safety: relies on CPython GIL for atomic dict operations.
# Under free-threaded Python (PEP 703, --disable-gil), a threading.Lock
# would be needed around cache-miss writes. Current async-first design
# means all access is single-threaded within an event loop.
_provider_config_cache: dict[str, ProviderConfig] = {}
_adapter_class_cache: dict[str, type[LLMProvider]] = {}
_global_config_cache: dict[str, Any] | None = None
_module_settings_cache: dict[str, dict[str, Any]] = {}
_vault_resolver_cache: Any | None = None


def clear_cache() -> None:
    """Reset all registry caches. Use in tests for isolation."""
    global _global_config_cache, _vault_resolver_cache
    _provider_config_cache.clear()
    _adapter_class_cache.clear()
    _global_config_cache = None
    _module_settings_cache.clear()
    _vault_resolver_cache = None
    from arcllm.modules.rate_limit import clear_buckets

    clear_buckets()

    import arcllm.modules.otel as _otel_mod

    _otel_mod._sdk_configured = False


def _get_adapter_class(provider_name: str) -> type[LLMProvider]:
    """Look up the adapter class by naming convention.

    Convention:
        provider_name -> module: arcllm.adapters.{provider_name}
        provider_name -> class:  {provider_name.title()}Adapter
    """
    if provider_name in _adapter_class_cache:
        return _adapter_class_cache[provider_name]

    module_path = f"arcllm.adapters.{provider_name}"
    try:
        module = importlib.import_module(module_path)
    except ImportError:
        raise ArcLLMConfigError(
            f"No adapter module found for provider '{provider_name}'. "
            f"Expected module: {module_path}"
        )

    class_name = f"{provider_name.title()}Adapter"
    adapter_class = getattr(module, class_name, None)
    if adapter_class is None:
        raise ArcLLMConfigError(
            f"No adapter class '{class_name}' found in module '{module_path}'"
        )

    _adapter_class_cache[provider_name] = adapter_class
    return adapter_class


def _resolve_module_config(
    module_name: str,
    kwarg_value: bool | dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Merge config.toml module settings with load_model() kwarg override.

    Resolution priority (highest first):
        1. kwarg=False  → disabled (returns None)
        2. kwarg={...}  → use kwarg dict (merged over config.toml defaults)
        3. kwarg=True    → use config.toml settings (or empty defaults)
        4. kwarg=None    → check config.toml enabled flag

    Returns:
        Module config dict if enabled, None if disabled.
    """
    global _global_config_cache
    if _global_config_cache is None:
        _global_config_cache = load_global_config()
        # Pre-extract module settings (avoids model_dump() per call)
        for name, cfg in _global_config_cache.modules.items():
            _module_settings_cache[name] = {
                k: v for k, v in cfg.model_dump().items() if k != "enabled"
            }

    # Get config.toml settings for this module
    module_cfg = _global_config_cache.modules.get(module_name)
    config_enabled = module_cfg.enabled if module_cfg else False
    config_settings = _module_settings_cache.get(module_name, {})

    # Resolve based on kwarg
    if kwarg_value is False:
        return None
    if kwarg_value is True:
        return config_settings
    if isinstance(kwarg_value, dict):
        # Kwarg dict overrides config.toml defaults
        merged = {**config_settings, **kwarg_value}
        return merged
    # kwarg_value is None — use config.toml enabled flag
    if config_enabled:
        return config_settings
    return None


def load_model(
    provider: str,
    model: str | None = None,
    *,
    retry: bool | dict[str, Any] | None = None,
    fallback: bool | dict[str, Any] | None = None,
    rate_limit: bool | dict[str, Any] | None = None,
    telemetry: bool | dict[str, Any] | None = None,
    audit: bool | dict[str, Any] | None = None,
    security: bool | dict[str, Any] | None = None,
    otel: bool | dict[str, Any] | None = None,
) -> LLMProvider:
    """Load a configured model object for the given provider.

    The returned adapter is a **long-lived object** — create it once and
    reuse it for many ``invoke()`` calls within your agent's lifecycle.
    Each call to ``load_model()`` creates a new httpx connection pool,
    so avoid calling it per-request.

    Recommended usage::

        async with load_model("anthropic") as model:
            resp = await model.invoke(messages, tools)

    Module kwargs control opt-in wrapping:
        - ``True``: enable with config.toml defaults
        - ``False``: disable (overrides config.toml)
        - ``dict``: enable with custom settings (merged over defaults)
        - ``None`` (default): use config.toml enabled flag

    Stacking order (outermost first): Otel → Telemetry → Audit → Security → Retry → Fallback → RateLimit → Adapter.

    Args:
        provider: Provider name (e.g., "anthropic", "openai").
            Must match a TOML file in providers/ and a module in adapters/.
        model: Model identifier. If None, uses default_model from provider config.
        retry: RetryModule configuration override.
        fallback: FallbackModule configuration override.
        rate_limit: RateLimitModule configuration override.
        telemetry: TelemetryModule configuration override. Pricing data is
            automatically injected from provider model metadata.
        audit: AuditModule configuration override. PII-safe metadata logging.
        security: SecurityModule configuration override. PII redaction + request signing.
        otel: OtelModule configuration override. OpenTelemetry distributed tracing.

    Returns:
        A configured LLMProvider instance ready for invoke().

    Raises:
        ArcLLMConfigError: On missing config, missing adapter, or invalid provider name.
    """
    # Load and cache provider config
    if provider not in _provider_config_cache:
        _provider_config_cache[provider] = load_provider_config(provider)
    config = _provider_config_cache[provider]

    # Resolve model name
    model_name = model or config.provider.default_model

    # Resolve API key via vault if configured
    global _global_config_cache
    if _global_config_cache is None:
        _global_config_cache = load_global_config()
        for name, cfg in _global_config_cache.modules.items():
            _module_settings_cache[name] = {
                k: v for k, v in cfg.model_dump().items() if k != "enabled"
            }

    global _vault_resolver_cache
    vault_cfg = _global_config_cache.vault
    if vault_cfg.backend:
        from arcllm.vault import VaultResolver

        if _vault_resolver_cache is None:
            _vault_resolver_cache = VaultResolver.from_config(
                vault_cfg.backend, vault_cfg.cache_ttl_seconds
            )
        vault_path = config.provider.vault_path
        api_key_env = config.provider.api_key_env
        resolved_key = _vault_resolver_cache.resolve_api_key(
            api_key_env, vault_path or None
        )
        # Set env var so adapter picks it up transparently
        os.environ[api_key_env] = resolved_key

    # Look up adapter class by convention (cached after first lookup)
    adapter_class = _get_adapter_class(provider)

    # Construct adapter
    result: LLMProvider = adapter_class(config, model_name)

    # Apply module wrapping (innermost first): RateLimit, Fallback, Retry
    rate_limit_config = _resolve_module_config("rate_limit", rate_limit)
    if rate_limit_config is not None:
        from arcllm.modules.rate_limit import RateLimitModule

        result = RateLimitModule(rate_limit_config, result)

    fallback_config = _resolve_module_config("fallback", fallback)
    if fallback_config is not None:
        from arcllm.modules.fallback import FallbackModule

        result = FallbackModule(fallback_config, result)

    retry_config = _resolve_module_config("retry", retry)
    if retry_config is not None:
        from arcllm.modules.retry import RetryModule

        result = RetryModule(retry_config, result)

    security_config = _resolve_module_config("security", security)
    if security_config is not None:
        from arcllm.modules.security import SecurityModule

        result = SecurityModule(security_config, result)

    audit_config = _resolve_module_config("audit", audit)
    if audit_config is not None:
        from arcllm.modules.audit import AuditModule

        result = AuditModule(audit_config, result)

    telemetry_config = _resolve_module_config("telemetry", telemetry)
    if telemetry_config is not None:
        from arcllm.modules.telemetry import TelemetryModule

        # Inject pricing from provider model metadata
        model_meta = config.models.get(model_name)
        if model_meta is not None:
            telemetry_config.setdefault(
                "cost_input_per_1m", model_meta.cost_input_per_1m
            )
            telemetry_config.setdefault(
                "cost_output_per_1m", model_meta.cost_output_per_1m
            )
            telemetry_config.setdefault(
                "cost_cache_read_per_1m", model_meta.cost_cache_read_per_1m
            )
            telemetry_config.setdefault(
                "cost_cache_write_per_1m", model_meta.cost_cache_write_per_1m
            )
        result = TelemetryModule(telemetry_config, result)

    otel_config = _resolve_module_config("otel", otel)
    if otel_config is not None:
        from arcllm.modules.otel import OtelModule

        result = OtelModule(otel_config, result)

    return result
