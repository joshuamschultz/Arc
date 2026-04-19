"""WebModule — web search and extraction tools for ArcAgent.

Implements the Module protocol.  On startup:
  1. Resolves provider API keys via vault resolver
  2. Constructs configured search + extract providers
  3. Registers ``web_search`` and ``web_extract`` tools in the ToolRegistry
  4. Enforces URL allowlist at federal tier

Security controls:
  - API keys resolved via vault (never hardcoded)
  - URL allowlist enforced for outbound requests (federal: required)
  - PII redaction applied to queries and extracted content (federal/enterprise)
  - Query hash (not plaintext) included in audit events (privacy)
  - Extracted content truncated at max_content_bytes

Spec: SPEC-018 T4.8
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from arcagent.modules.web.config import WebConfig
from arcagent.modules.web.errors import ContentTooLarge, URLNotAllowed
from arcagent.modules.web.protocols import (
    ExtractResult,
    SearchHit,
    WebExtractProvider,
    WebSearchProvider,
)
from arcagent.modules.web.url_policy import is_url_allowed

_logger = logging.getLogger("arcagent.modules.web")


def _hash_query(query: str) -> str:
    """Return first 16 hex chars of SHA-256(query) for audit logs.

    Query content is PII-sensitive at federal/enterprise tier — we log the
    hash, not the plaintext, in audit events.  Callers still pass the full
    query to the provider (after PII redaction).
    """
    return hashlib.sha256(query.encode()).hexdigest()[:16]


def _redact_if_enabled(text: str, *, enabled: bool) -> str:
    """Apply PII redaction when enabled.

    Uses arcllm._pii directly so we don't take on an arcllm module-bus
    dependency.  The RegexPiiDetector is stateless — safe to instantiate
    per-call.

    arcllm is listed in mypy ``ignore_missing_imports`` — the import
    resolves at runtime but mypy cannot see the types.  We annotate
    the return value explicitly to restore the str contract.
    """
    if not enabled:
        return text
    # arcllm is covered by ignore_missing_imports; no type: ignore needed
    from arcllm._pii import RegexPiiDetector, redact_text

    detector = RegexPiiDetector()
    matches = detector.detect(text)
    if not matches:
        return text
    # redact_text returns str; explicit annotation restores strict checking
    result: str = str(redact_text(text, matches))
    return result


def _truncate_content(content: str, max_bytes: int, url: str) -> str:
    """Truncate content to max_bytes, logging a warning if truncation occurs.

    Content is truncated at byte boundary, then decoded safely.
    Returns original content when no truncation is needed.
    """
    encoded = content.encode("utf-8")
    if len(encoded) <= max_bytes:
        return content
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
    actual = len(encoded)
    err = ContentTooLarge(url=url, actual_bytes=actual, max_bytes=max_bytes)
    _logger.warning("%s", err)
    return truncated


class WebModule:
    """Module Bus subscriber providing web_search and web_extract tools.

    Implements the Module protocol.  Providers are injected; API keys are
    resolved by the caller (typically via vault resolver) before construction.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        workspace: Path = Path("."),
    ) -> None:
        self._raw_config = config or {}
        self._config = WebConfig(**self._raw_config)
        self._workspace = workspace.resolve()
        self._search_provider: WebSearchProvider | None = None
        self._extract_provider: WebExtractProvider | None = None
        self._bus: Any = None

    @property
    def name(self) -> str:
        """Module name used in bus events and config keys."""
        return "web"

    def set_search_provider(self, provider: WebSearchProvider) -> None:
        """Inject a search provider (used in tests and by startup)."""
        self._search_provider = provider

    def set_extract_provider(self, provider: WebExtractProvider) -> None:
        """Inject an extract provider (used in tests and by startup)."""
        self._extract_provider = provider

    async def startup(self, ctx: Any) -> None:
        """Build providers, validate config, and register tools.

        At federal tier, an empty ``url_allowlist`` is a hard error —
        the module will not start without at least one allowlist pattern.
        """
        self._bus = ctx.bus
        tier = self._config.tier

        # Federal tier: non-empty allowlist is required
        if tier == "federal" and not self._config.url_allowlist:
            raise RuntimeError(
                "[federal] web module requires a non-empty url_allowlist. "
                "Configure [modules.web] url_allowlist in arcagent.toml."
            )

        # Build providers from resolved API keys
        if self._search_provider is None:
            self._search_provider = await self._build_search_provider(ctx)
        if self._extract_provider is None:
            self._extract_provider = await self._build_extract_provider(ctx)

        # Register tools in the ToolRegistry
        _register_web_search_tool(ctx.tool_registry, self)
        _register_web_extract_tool(ctx.tool_registry, self)

        await ctx.bus.emit(
            "web.started",
            {
                "search_provider": self._config.search_provider,
                "extract_provider": self._config.extract_provider,
                "tier": tier,
                "allowlist_size": len(self._config.url_allowlist),
            },
        )
        _logger.info(
            "Web module started (search=%s, extract=%s, tier=%s)",
            self._config.search_provider,
            self._config.extract_provider,
            tier,
        )

    async def _build_search_provider(self, ctx: Any) -> WebSearchProvider:
        """Resolve API key and construct the configured search provider."""
        name = self._config.search_provider
        api_key = await _resolve_api_key(name, ctx, self._config.tier)
        # Concrete provider classes satisfy WebSearchProvider via duck-typing;
        # _make_provider returns Any to avoid Union-narrowing mypy complexity.
        provider: WebSearchProvider = _make_provider(name, api_key, self._config.request_timeout_s)
        return provider

    async def _build_extract_provider(self, ctx: Any) -> WebExtractProvider:
        """Resolve API key and construct the configured extract provider."""
        name = self._config.extract_provider
        api_key = await _resolve_api_key(name, ctx, self._config.tier)
        provider: WebExtractProvider = _make_provider(
            name, api_key, self._config.request_timeout_s
        )
        return provider

    async def web_search(self, query: str, limit: int = 10) -> list[SearchHit]:
        """Execute a web search with PII redaction and audit logging.

        Args:
            query: Search query (PII-redacted at federal/enterprise before sending).
            limit: Maximum results.

        Returns:
            List of SearchHit instances.
        """
        cfg = self._config
        pii_on = cfg.pii_redaction_enabled or cfg.tier == "federal"
        redacted_query = _redact_if_enabled(query, enabled=pii_on)
        query_hash = _hash_query(redacted_query)

        if self._search_provider is None:
            from arcagent.modules.web.errors import SearchFailed

            raise SearchFailed("WebModule.web_search called before startup")

        results = await self._search_provider.search(redacted_query, limit=limit)

        if self._bus is not None:
            await self._bus.emit(
                "web.search",
                {
                    "provider": cfg.search_provider,
                    "query_hash": query_hash,
                    "result_count": len(results),
                },
            )

        return results

    async def web_extract(self, url: str) -> ExtractResult:
        """Extract web content with URL policy enforcement and size cap.

        Args:
            url: Target URL.

        Returns:
            ExtractResult with truncated content if over size cap.

        Raises:
            URLNotAllowed: When URL is denied by tier policy.
        """
        cfg = self._config

        # Enforce URL allowlist BEFORE any network request
        if not is_url_allowed(url, allowlist=cfg.url_allowlist, tier=cfg.tier):
            if self._bus is not None:
                await self._bus.emit(
                    "web.url_denied",
                    {"url": url, "tier": cfg.tier},
                )
            raise URLNotAllowed(url=url, tier=cfg.tier)

        if self._extract_provider is None:
            from arcagent.modules.web.errors import ExtractFailed

            raise ExtractFailed("WebModule.web_extract called before startup")

        result = await self._extract_provider.extract(url)

        # Apply content size cap — truncate, do not silently drop
        content = _truncate_content(result.content, cfg.max_content_bytes, url)

        # Apply PII redaction on extracted content
        pii_on = cfg.pii_redaction_enabled or cfg.tier == "federal"
        content = _redact_if_enabled(content, enabled=pii_on)

        final = result.model_copy(update={"content": content})

        # URL already passed allowlist check above; re-evaluate for audit log
        allowlist_match = is_url_allowed(url, allowlist=cfg.url_allowlist, tier=cfg.tier)

        if self._bus is not None:
            await self._bus.emit(
                "web.extract",
                {
                    "provider": cfg.extract_provider,
                    "url": url,
                    "content_size_bytes": len(content.encode("utf-8")),
                    "allowlist_match": allowlist_match,
                },
            )

        return final

    async def shutdown(self) -> None:
        """Clean up provider connections."""
        _logger.info("Web module shut down")


# ---------------------------------------------------------------------------
# Tool registration helpers
# ---------------------------------------------------------------------------


def _register_web_search_tool(tool_registry: Any, module: WebModule) -> None:
    """Register the web_search tool in the tool registry.

    The try/except allows graceful degradation when arcrun is unavailable
    (e.g. in isolated unit tests that do not wire the full ToolRegistry).
    """

    async def _web_search(params: dict[str, Any], ctx: Any) -> Any:
        """Execute a web search and return results as list of dicts."""
        query: str = params["query"]
        limit: int = int(params.get("limit", 10))
        hits = await module.web_search(query, limit=limit)
        return [h.model_dump() for h in hits]

    try:
        # arcrun is covered by ignore_missing_imports in pyproject.toml
        from arcrun.types import Tool

        tool = Tool(
            name="web_search",
            description=(
                "Search the web for information. Returns a list of results "
                "with URL, title, and snippet."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of results",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
            execute=_web_search,
        )
        tool_registry.register(tool)
    except Exception as exc:
        _logger.warning("Could not register web_search tool: %s", exc)


def _register_web_extract_tool(tool_registry: Any, module: WebModule) -> None:
    """Register the web_extract tool in the tool registry.

    The try/except allows graceful degradation when arcrun is unavailable
    (e.g. in isolated unit tests that do not wire the full ToolRegistry).
    """

    async def _web_extract(params: dict[str, Any], ctx: Any) -> Any:
        """Extract content from a web URL and return as dict."""
        url: str = params["url"]
        result = await module.web_extract(url)
        return result.model_dump()

    try:
        from arcrun.types import Tool

        tool = Tool(
            name="web_extract",
            description="Extract and return the full content of a web page as Markdown.",
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to extract"},
                },
                "required": ["url"],
            },
            execute=_web_extract,
        )
        tool_registry.register(tool)
    except Exception as exc:
        _logger.warning("Could not register web_extract tool: %s", exc)


# ---------------------------------------------------------------------------
# Provider construction helpers
# ---------------------------------------------------------------------------


async def _resolve_api_key(provider_name: str, ctx: Any, tier: str) -> str:
    """Resolve API key for the named provider using vault resolver."""
    secret_name_map: dict[str, str] = {
        "parallel": "parallel_api_key",
        "firecrawl": "firecrawl_api_key",
        "tavily": "tavily_api_key",
    }
    secret_name = secret_name_map.get(provider_name, f"{provider_name}_api_key")
    env_var_map: dict[str, str] = {
        "parallel": "PARALLEL_API_KEY",
        "firecrawl": "FIRECRAWL_API_KEY",
        "tavily": "TAVILY_API_KEY",
    }
    env_var: str | None = env_var_map.get(provider_name)

    try:
        from arcagent.modules.vault.resolver import resolve_secret

        vault_backend = getattr(ctx, "vault_backend", None)
        resolved: str = await resolve_secret(
            secret_name,
            tier=tier,
            backend=vault_backend,
            env_fallback_var=env_var,
        )
        return resolved
    except Exception as exc:
        from arcagent.modules.web.errors import ProviderConfigMissing

        raise ProviderConfigMissing(provider_name, secret_name) from exc


def _make_provider(name: str, api_key: str, timeout_s: float) -> Any:
    """Construct the named provider adapter.

    Returns Any so callers can assign to either WebSearchProvider or
    WebExtractProvider without mypy Union narrowing errors.  The concrete
    classes (ParallelProvider, FirecrawlProvider, TavilyProvider) all
    satisfy both Protocols via duck-typing.
    """
    from arcagent.modules.web.providers.firecrawl import FirecrawlProvider
    from arcagent.modules.web.providers.parallel import ParallelProvider
    from arcagent.modules.web.providers.tavily import TavilyProvider

    provider_map: dict[str, Any] = {
        "parallel": ParallelProvider,
        "firecrawl": FirecrawlProvider,
        "tavily": TavilyProvider,
    }
    cls = provider_map.get(name)
    if cls is None:
        raise ValueError(f"Unknown web provider: {name!r}")
    return cls.create(api_key=api_key, timeout_s=timeout_s)


__all__ = ["WebModule"]
