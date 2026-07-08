"""Provider registry — convention-based adapter discovery and load_model()."""

import importlib
import logging
import threading
from collections.abc import Callable
from typing import Any, cast

from arcllm.adapters.base import BaseAdapter
from arcllm.config import (
    EndpointConfig,
    GlobalConfig,
    ProviderConfig,
    _validate_provider_name,
    load_global_config,
    load_provider_config,
)
from arcllm.exceptions import ArcLLMConfigError
from arcllm.types import LLMProvider

logger = logging.getLogger(__name__)

# Module-level caches: loaded once per provider, reused across calls.
# Lock protects cache-miss writes for thread safety (PEP 703 ready).
_cache_lock = threading.Lock()
_provider_config_cache: dict[str, ProviderConfig] = {}
_adapter_class_cache: dict[str, type[BaseAdapter]] = {}
_global_config_cache: GlobalConfig | None = None
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

    from arcllm.modules.load_balancer import clear_pools

    clear_pools()

    from arcllm.modules.otel import reset_sdk

    reset_sdk()

    from arcllm.modules.telemetry import clear_budgets, clear_global_defaults

    clear_budgets()
    clear_global_defaults()


def _get_adapter_class(provider_name: str) -> type[BaseAdapter]:
    """Look up the adapter class by naming convention.

    Convention:
        provider_name -> module: arcllm.adapters.{provider_name}
        provider_name -> class:  {provider_name.title()}Adapter
    """
    _validate_provider_name(provider_name)

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
        adapter_class: type[BaseAdapter] | None = getattr(module, class_name, None)
        if adapter_class is None:
            raise ArcLLMConfigError(
                f"No adapter class '{class_name}' found in module '{module_path}'"
            )

        _adapter_class_cache[provider_name] = adapter_class
        return adapter_class


def _ensure_global_config() -> GlobalConfig:
    """Load and cache global config + module settings on first access.

    Idempotent accessor: read sites call this to get the loaded config
    rather than touching the ``| None`` global directly.
    """
    global _global_config_cache
    cached = _global_config_cache
    if cached is not None:
        return cached
    with _cache_lock:
        cached = _global_config_cache
        if cached is None:
            cached = load_global_config()
            for name, cfg in cached.modules.items():
                _module_settings_cache[name] = {
                    k: v for k, v in cfg.model_dump().items() if k != "enabled"
                }
            _global_config_cache = cached
    return cached


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
    # Get config.toml settings for this module
    module_cfg = _ensure_global_config().modules.get(module_name)
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


def _construct_adapter(
    provider_name: str,
    resolved_model: str,
    provider_config: ProviderConfig,
    vault_cfg: Any,
    vault_resolver: Any,
) -> LLMProvider:
    """Shared vault-key-resolution + adapter-construction tail (L8, D-457).

    Single key-handling code path for every adapter build site — the
    primary adapter, routing-rule adapters, and load-balancer pool
    endpoints all resolve their vault key and construct their
    ``BaseAdapter`` here, never a second, less-audited copy of this logic.
    """
    resolved_api_key: str | None = None
    if vault_cfg.backend and vault_resolver is not None:
        vault_path = provider_config.provider.vault_path
        api_key_env = provider_config.provider.api_key_env
        resolved_api_key = vault_resolver.resolve_api_key(api_key_env, vault_path or None)

    adapter_class = _get_adapter_class(provider_name)
    return adapter_class(provider_config, resolved_model, resolved_api_key=resolved_api_key)


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
    return _construct_adapter(provider_name, resolved_model, config, vault_cfg, vault_resolver)


def _endpoint_identity(endpoint: EndpointConfig) -> str:
    """Stable identity string for a pool endpoint: base_url + key *source name*.

    Never includes a resolved secret value (D-457, FR-18) — only the
    env var name or vault path, i.e. where the key comes from, not the
    key itself. Used as the per-endpoint health key and as an input to
    the pool's identity hash (SPEC-017).
    """
    key_source = endpoint.api_key_env or endpoint.vault_path
    return f"{endpoint.base_url}::{key_source}"


def _build_adapter_for_endpoint(
    provider_name: str,
    model_name: str | None,
    endpoint: EndpointConfig,
    vault_cfg: Any,
    vault_resolver: Any,
) -> LLMProvider:
    """Build one adapter for a single load-balancing pool endpoint.

    Clones the resolved ``ProviderConfig`` with the endpoint's
    ``base_url``/key-source overridden, then reuses the exact same vault
    resolution + ``BaseAdapter`` construction as ``_build_adapter`` — no
    second, less-audited key-handling code path (D-457, SPEC-017 ADR-8).
    """
    if provider_name not in _provider_config_cache:
        _provider_config_cache[provider_name] = load_provider_config(provider_name)
    base_config = _provider_config_cache[provider_name]
    resolved_model = model_name or base_config.provider.default_model

    endpoint_provider_settings = base_config.provider.model_copy(
        update={
            "base_url": endpoint.base_url,
            "api_key_env": endpoint.api_key_env,
            "vault_path": endpoint.vault_path,
        }
    )
    endpoint_config = base_config.model_copy(update={"provider": endpoint_provider_settings})
    return _construct_adapter(
        provider_name, resolved_model, endpoint_config, vault_cfg, vault_resolver
    )


# Canonical set of arcllm module kwarg names accepted by load_model(). This is
# the single source of truth — callers that validate per-module overrides
# (e.g. arcagent's load_eval_model) import this instead of re-declaring the
# list, so adding a module here can't silently drift from downstream guards.
# test_registry.py asserts this matches load_model's signature.
MODULE_NAMES: frozenset[str] = frozenset(
    {
        "routing",
        "retry",
        "fallback",
        "rate_limit",
        "load_balance",
        "circuit_breaker",
        "telemetry",
        "queue",
        "audit",
        "security",
        "injection",
        "guardrails",
        "otel",
    }
)


# Single-config module wrappers: every one is constructed uniformly as
# ``Module(resolved_config_dict, wrapped)``. Only circuit_breaker (on_event
# threading) and telemetry (pricing/budget/encryption prep) are bespoke and
# stay explicit in load_model. The STACKING ORDER is load-bearing and lives
# in load_model's call sequence, not here (ADR-422, ADR-430).
_GENERIC_MODULES: dict[str, tuple[str, str]] = {
    "rate_limit": ("arcllm.modules.rate_limit", "RateLimitModule"),
    "fallback": ("arcllm.modules.fallback", "FallbackModule"),
    "retry": ("arcllm.modules.retry", "RetryModule"),
    "security": ("arcllm.modules.security", "SecurityModule"),
    "injection": ("arcllm.modules.injection", "InjectionModule"),
    "guardrails": ("arcllm.modules.guardrails", "GuardrailsModule"),
    "audit": ("arcllm.modules.audit", "AuditModule"),
    "queue": ("arcllm.modules.queue", "QueueModule"),
    "otel": ("arcllm.modules.otel", "OtelModule"),
}


def _wrap_generic(
    result: LLMProvider,
    config_name: str,
    kwarg_value: bool | dict[str, Any] | None,
) -> LLMProvider:
    """Wrap *result* in the named single-config module when it is enabled.

    Lazy-imports the module so unused wrappers are never loaded, then
    constructs ``Module(resolved_config, wrapped)``. No-op (returns
    *result* unchanged) when the module resolves to disabled.
    """
    cfg = _resolve_module_config(config_name, kwarg_value)
    if cfg is None:
        return result
    module_path, class_name = _GENERIC_MODULES[config_name]
    module = importlib.import_module(module_path)
    module_cls = cast(
        Callable[[dict[str, Any], LLMProvider], LLMProvider],
        getattr(module, class_name),
    )
    return module_cls(cfg, result)


def _apply_telemetry(
    result: LLMProvider,
    telemetry_config: dict[str, Any],
    config: ProviderConfig,
    model_name: str,
    *,
    budget_scope: str | None,
    on_event: Callable[[Any], None] | None,
    trace_store: Any | None,
    agent_label: str | None,
    lineage: dict[str, Any] | None,
    vault_cfg: Any,
) -> LLMProvider:
    """Build and stack the TelemetryModule with pricing/budget/encryption prep.

    Kept out of ``load_model`` because its config threading (pricing from
    model metadata, budget scope, trace wiring, one-time encryption-key
    resolution) is genuinely bespoke, unlike the uniform generic wrappers.
    """
    from arcllm.modules.telemetry import TelemetryModule

    # Inject pricing from provider model metadata
    model_meta = config.models.get(model_name)
    if model_meta is not None:
        telemetry_config.setdefault("cost_input_per_1m", model_meta.cost_input_per_1m)
        telemetry_config.setdefault("cost_output_per_1m", model_meta.cost_output_per_1m)
        telemetry_config.setdefault("cost_cache_read_per_1m", model_meta.cost_cache_read_per_1m)
        telemetry_config.setdefault("cost_cache_write_per_1m", model_meta.cost_cache_write_per_1m)

    # Inject budget_scope from load_model() kwarg into telemetry config
    if budget_scope is not None:
        telemetry_config["budget_scope"] = budget_scope

    # Source default_max_tokens from global config defaults
    telemetry_config.setdefault("default_max_tokens", _ensure_global_config().defaults.max_tokens)

    # Thread trace_store, on_event, agent_label, and lineage into config
    if on_event is not None:
        telemetry_config["on_event"] = on_event
    if trace_store is not None:
        telemetry_config["trace_store"] = trace_store
    if agent_label is not None:
        telemetry_config["agent_label"] = agent_label
    if lineage is not None:
        telemetry_config["lineage"] = lineage

    # SPEC-016 D-440: retention purge operates on a JSONLTraceStore's
    # directory, constructed independently of load_model() (the store
    # owns agent_root). TelemetryModule doesn't consume it — drop it
    # here rather than teaching TelemetryModule an unused config key.
    # Callers read arcllm.config.load_telemetry_retention_config() and
    # wire it into their own JSONLTraceStore construction.
    telemetry_config.pop("retention", None)

    # SPEC-016 D-438/D-447: resolve the wrapping key ONCE, at
    # construction (AU-2 — tier posture must flow through construction,
    # never be re-resolved per call). Reuses the shared VaultResolver
    # (with its TTL cache) when one is already configured; falls back to
    # an env-only resolver otherwise (dev/personal, no vault backend).
    encryption_cfg = telemetry_config.get("encryption") or {}
    if encryption_cfg.get("enabled"):
        from arcllm.vault import VaultResolver

        wrap_resolver = _vault_resolver_cache or VaultResolver(
            backend=None, cache_ttl_seconds=vault_cfg.cache_ttl_seconds
        )
        telemetry_config["encryption_key_secret"] = wrap_resolver.resolve_api_key(
            encryption_cfg.get("key_env", "ARCLLM_TRACE_WRAP_KEY"),
            encryption_cfg.get("key_ref") or None,
        )

    return TelemetryModule(telemetry_config, result)


def load_model(
    provider: str,
    model: str | None = None,
    *,
    budget_scope: str | None = None,
    on_event: Callable[[Any], None] | None = None,
    trace_store: Any | None = None,
    agent_label: str | None = None,
    lineage: dict[str, Any] | None = None,
    routing: bool | dict[str, Any] | None = None,
    retry: bool | dict[str, Any] | None = None,
    fallback: bool | dict[str, Any] | None = None,
    rate_limit: bool | dict[str, Any] | None = None,
    load_balance: bool | dict[str, Any] | None = None,
    circuit_breaker: bool | dict[str, Any] | None = None,
    telemetry: bool | dict[str, Any] | None = None,
    queue: bool | dict[str, Any] | None = None,
    audit: bool | dict[str, Any] | None = None,
    security: bool | dict[str, Any] | None = None,
    injection: bool | dict[str, Any] | None = None,
    guardrails: bool | dict[str, Any] | None = None,
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
        Otel → Queue → Telemetry → Audit → Guardrails → Injection →
        Security → CircuitBreaker → Retry → Fallback → RateLimit →
        [Router|LoadBalancer|Adapter]. (ADR-430: Injection sits above
        Security so it sees pre-redaction text; Guardrails sits just
        inside Audit so it validates the final post-Retry/Fallback
        response and the audit trail records its verdict.)

    Args:
        provider: Provider name (e.g., "anthropic", "openai").
            Must match a TOML file in providers/ and a module in adapters/.
        model: Model identifier. If None, uses default_model from provider config.
        budget_scope: Budget tracking scope (e.g., "agent:agent-007"). Required
            when budget limits are configured in telemetry.
        on_event: Optional callback fired after every invoke() with a TraceRecord.
            Fires OUTSIDE any locks. Zero overhead when None.
        trace_store: Optional TraceStore for persistent recording. Records appended
            after every invoke(). Independent of on_event — either, both, or neither.
        agent_label: Label attached to TraceRecords for multi-agent identification.
        lineage: Optional provenance token (template source, RAG documents,
            variable substitution) attached VERBATIM to every TraceRecord's
            ``lineage`` field. arcllm never constructs or infers lineage —
            arcrun/arcagent build it and pass it through here (SPEC-016 D-443).
        routing: RoutingModule configuration override. When enabled, replaces the
            single adapter with a classification-based router.
        retry: RetryModule configuration override.
        fallback: FallbackModule configuration override.
        rate_limit: RateLimitModule configuration override.
        load_balance: LoadBalancerModule configuration override. When enabled and
            the provider TOML declares an ``[[endpoints]]`` pool, replaces the
            single adapter with a load-balanced pool (weighted round-robin,
            health-aware, or sticky). No-op when no pool is configured.
        circuit_breaker: CircuitBreakerModule configuration override. Per-provider
            circuit breaker with CLOSED/OPEN/HALF_OPEN state machine.
        telemetry: TelemetryModule configuration override. Pricing data is
            automatically injected from provider model metadata.
        queue: QueueModule configuration override. Bounded concurrency with
            backpressure and send-time timeouts for LLM calls.
        audit: AuditModule configuration override. PII-safe metadata logging.
        security: SecurityModule configuration override. PII redaction + request signing.
        injection: InjectionModule configuration override. Opt-in, OFF by default.
            Scans inbound user + tool-result content for prompt-injection patterns
            before the provider call (OWASP LLM01, ASI06).
        guardrails: GuardrailsModule configuration override. Opt-in per call.
            Validates the final resolved response's structure (JSON schema,
            regex allow/deny, max length, banned-content stop-list) (OWASP LLM05).
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
    global _vault_resolver_cache
    vault_cfg = _ensure_global_config().vault
    if vault_cfg.backend and _vault_resolver_cache is None:
        from arcllm.vault import VaultResolver

        # Forward only the optional config fields the operator actually set
        # to the backend. Each backend chooses which kwargs it accepts —
        # passing only set fields means a backend ignoring `region` doesn't
        # see a confusing empty-string default. See ADR-005.
        backend_kwargs: dict[str, object] = {}
        if vault_cfg.region:
            backend_kwargs["region_name"] = vault_cfg.region
        if vault_cfg.url:
            backend_kwargs["url"] = vault_cfg.url
        _vault_resolver_cache = VaultResolver.from_config(
            vault_cfg.backend,
            vault_cfg.cache_ttl_seconds,
            **backend_kwargs,
        )

    # Check if routing is enabled — if so, create a RoutingModule instead of
    # a single adapter. Router replaces adapter at innermost stack position.
    # LoadBalancer competes for the same innermost slot (SPEC-017 ADR-7) —
    # both are "Router-like" innermost-replacers; routing takes priority
    # when both happen to be configured (routing decides provider/model,
    # a strictly outer concern to endpoint selection within one provider).
    routing_config = _resolve_module_config("routing", routing)
    if routing_config is not None and routing_config.get("rules"):
        from arcllm.modules.routing import RoutingModule

        rules: dict[str, Any] = routing_config.get("rules", {})
        adapters: dict[str, LLMProvider] = {}
        for classification, rule in rules.items():
            rule_provider = rule.get("provider")
            if not rule_provider:
                raise ArcLLMConfigError(f"Routing rule '{classification}' missing 'provider'")
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
        lb_config = _resolve_module_config("load_balance", load_balance)
        if lb_config is not None and config.endpoints:
            from arcllm.modules.load_balancer import LoadBalancerModule, PoolEndpoint

            pool_endpoints = [
                PoolEndpoint(
                    adapter=_build_adapter_for_endpoint(
                        provider, model_name, ep, vault_cfg, _vault_resolver_cache
                    ),
                    weight=ep.weight,
                    endpoint_id=_endpoint_identity(ep),
                )
                for ep in config.endpoints
                if ep.weight > 0
            ]
            result = LoadBalancerModule(lb_config, pool_endpoints, provider)
        else:
            if lb_config is not None and not config.endpoints:
                logger.info(
                    "load_balance enabled but no endpoints configured for '%s'; "
                    "using single provider",
                    provider,
                )
            result = _build_adapter(provider, model_name, vault_cfg, _vault_resolver_cache)

    # Apply module wrapping, innermost first. The STACKING ORDER below is
    # load-bearing: injection sits ABOVE security so it scans the ORIGINAL
    # inbound text before PII/secret redaction can obscure encoded attack
    # signal (ADR-422); guardrails sits just inside audit so it validates the
    # response the audit trail also records (ADR-430). Do not reorder.
    result = _wrap_generic(result, "rate_limit", rate_limit)
    result = _wrap_generic(result, "fallback", fallback)
    result = _wrap_generic(result, "retry", retry)

    cb_config = _resolve_module_config("circuit_breaker", circuit_breaker)
    if cb_config is not None:
        from arcllm.modules.circuit_breaker import CircuitBreakerModule

        if on_event is not None:
            cb_config["on_event"] = on_event
        result = CircuitBreakerModule(cb_config, result)

    result = _wrap_generic(result, "security", security)
    result = _wrap_generic(result, "injection", injection)
    result = _wrap_generic(result, "guardrails", guardrails)
    result = _wrap_generic(result, "audit", audit)

    telemetry_config = _resolve_module_config("telemetry", telemetry)
    if telemetry_config is not None:
        result = _apply_telemetry(
            result,
            telemetry_config,
            config,
            model_name,
            budget_scope=budget_scope,
            on_event=on_event,
            trace_store=trace_store,
            agent_label=agent_label,
            lineage=lineage,
            vault_cfg=vault_cfg,
        )

    result = _wrap_generic(result, "queue", queue)
    result = _wrap_generic(result, "otel", otel)

    return result
