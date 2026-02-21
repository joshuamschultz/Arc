"""Provider registry — convention-based adapter discovery and load_model()."""

import importlib
import threading
from typing import Any

from arcllm.config import ProviderConfig, load_global_config, load_provider_config
from arcllm.exceptions import ArcLLMConfigError
from arcllm.types import LLMProvider

# Module-level caches: loaded once per provider, reused across calls.
# Lock protects cache-miss writes for thread safety (PEP 703 ready).
_cache_lock = threading.Lock()
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

    from arcllm.modules.otel import reset_sdk

    reset_sdk()

    from arcllm.modules.telemetry import clear_budgets

    clear_budgets()


def _get_adapter_class(provider_name: str) -> type[LLMProvider]:
    """Look up the adapter class by naming convention.

    Convention:
        provider_name -> module: arcllm.adapters.{provider_name}
        provider_name -> class:  {provider_name.title()}Adapter
    """
    if provider_name in _adapter_class_cache:
        return _adapter_class_cache[provider_name]

    with _cache_lock:
        # Double-check after acquiring lock
        if provider_name in _adapter_class_cache:
            return _adapter_class_cache[provider_name]

        module_path = f"arcllm.adapters.{provider_name}"
        try:
            module = importlib.import_module(module_path)
        except ImportError as e:
            raise ArcLLMConfigError(
                f"No adapter module found for provider '{provider_name}'. "
                f"Expected module: {module_path}"
            ) from e

        class_name = f"{provider_name.title()}Adapter"
        adapter_class = getattr(module, class_name, None)
        if adapter_class is None:
            raise ArcLLMConfigError(
                f"No adapter class '{class_name}' found in module '{module_path}'"
            )

        _adapter_class_cache[provider_name] = adapter_class
        return adapter_class


def _ensure_global_config() -> Any:
    """Load and cache global config + module settings on first access."""
    global _global_config_cache
    if _global_config_cache is None:
        with _cache_lock:
            if _global_config_cache is None:
                _global_config_cache = load_global_config()
                for name, cfg in _global_config_cache.modules.items():
                    _module_settings_cache[name] = {
                        k: v for k, v in cfg.model_dump().items() if k != "enabled"
                    }
    return _global_config_cache


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
    _ensure_global_config()

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
        return {**config_settings, **kwarg_value}
    # kwarg_value is None — use config.toml enabled flag
    if config_enabled:
        return config_settings
    return None


def _build_adapter(
    provider_name: str,
    model_name: str | None,
    vault_cfg: Any,
    vault_resolver: Any,
) -> LLMProvider:
    """Build a single adapter for *provider_name*, resolving config and vault key.

    Eliminates duplication between the primary adapter path and routing
    rule adapter construction.
    """
    if provider_name not in _provider_config_cache:
        _provider_config_cache[provider_name] = load_provider_config(provider_name)
    config = _provider_config_cache[provider_name]
    resolved_model = model_name or config.provider.default_model

    resolved_api_key: str | None = None
    if vault_cfg.backend and vault_resolver is not None:
        vault_path = config.provider.vault_path
        api_key_env = config.provider.api_key_env
        resolved_api_key = vault_resolver.resolve_api_key(
            api_key_env, vault_path or None
        )

    adapter_class = _get_adapter_class(provider_name)
    return adapter_class(config, resolved_model, resolved_api_key=resolved_api_key)


def load_model(
    provider: str,
    model: str | None = None,
    *,
    budget_scope: str | None = None,
    routing: bool | dict[str, Any] | None = None,
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

    Stacking order (outermost first):
        Otel → Telemetry → Audit → Security → Retry → Fallback → RateLimit → [Router|Adapter].

    Args:
        provider: Provider name (e.g., "anthropic", "openai").
            Must match a TOML file in providers/ and a module in adapters/.
        model: Model identifier. If None, uses default_model from provider config.
        budget_scope: Budget tracking scope (e.g., "agent:agent-007"). Required
            when budget limits are configured in telemetry.
        routing: RoutingModule configuration override. When enabled, replaces the
            single adapter with a classification-based router.
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

    # Ensure global config + vault resolver are initialized
    _ensure_global_config()
    global _vault_resolver_cache
    vault_cfg = _global_config_cache.vault
    if vault_cfg.backend and _vault_resolver_cache is None:
        from arcllm.vault import VaultResolver

        _vault_resolver_cache = VaultResolver.from_config(
            vault_cfg.backend, vault_cfg.cache_ttl_seconds
        )

    # Check if routing is enabled — if so, create a RoutingModule instead of
    # a single adapter. Router replaces adapter at innermost stack position.
    routing_config = _resolve_module_config("routing", routing)
    if routing_config is not None and routing_config.get("rules"):
        from arcllm.modules.routing import RoutingModule

        rules: dict[str, Any] = routing_config.get("rules", {})
        adapters: dict[str, LLMProvider] = {}
        for classification, rule in rules.items():
            rule_provider = rule.get("provider")
            if not rule_provider:
                raise ArcLLMConfigError(
                    f"Routing rule '{classification}' missing 'provider'"
                )
            adapters[classification] = _build_adapter(
                rule_provider, rule.get("model"), vault_cfg, _vault_resolver_cache
            )

        result: LLMProvider = RoutingModule(
            {
                "enforcement": routing_config.get("enforcement", "block"),
                "default_classification": routing_config.get(
                    "default_classification", "unclassified"
                ),
            },
            adapters,
        )
    else:
        result = _build_adapter(provider, model_name, vault_cfg, _vault_resolver_cache)

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
            telemetry_config.setdefault("cost_input_per_1m", model_meta.cost_input_per_1m)
            telemetry_config.setdefault("cost_output_per_1m", model_meta.cost_output_per_1m)
            telemetry_config.setdefault(
                "cost_cache_read_per_1m", model_meta.cost_cache_read_per_1m
            )
            telemetry_config.setdefault(
                "cost_cache_write_per_1m", model_meta.cost_cache_write_per_1m
            )

        # Inject budget_scope from load_model() kwarg into telemetry config
        if budget_scope is not None:
            telemetry_config["budget_scope"] = budget_scope

        # Source default_max_tokens from global config defaults
        telemetry_config.setdefault(
            "default_max_tokens", _global_config_cache.defaults.max_tokens
        )

        result = TelemetryModule(telemetry_config, result)

    otel_config = _resolve_module_config("otel", otel)
    if otel_config is not None:
        from arcllm.modules.otel import OtelModule

        result = OtelModule(otel_config, result)

    return result
