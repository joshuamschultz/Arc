"""Per-agent web module runtime context.

The web module's tools share state — the resolved ``WebConfig``,
lazily-built provider clients, URL policy, and the telemetry sink for
audit events. Decorator-stamped functions can't carry that state in a
closure, so it lives in a module-level :class:`_State` instance
configured by the agent at startup.

Mirrors the pattern in :mod:`arcagent.modules.voice._runtime` and
:mod:`arcagent.builtins.capabilities._runtime` (single-agent-per-process
model).

Federal tier validation runs at :func:`configure` time so misconfiguration
is caught before any network request is attempted — fail fast, fail loud.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arcagent.modules.web.config import WebConfig
from arcagent.modules.web.protocols import WebExtractProvider, WebSearchProvider

_logger = logging.getLogger("arcagent.modules.web._runtime")


@dataclass
class _State:
    """Mutable runtime state shared across web tools."""

    config: WebConfig
    telemetry: Any
    workspace: Path
    agent_name: str
    search_provider: WebSearchProvider | None = field(default=None)
    extract_provider: WebExtractProvider | None = field(default=None)


_state: _State | None = None


def configure(
    *,
    config: dict[str, Any] | None = None,
    telemetry: Any = None,
    workspace: Path = Path("."),
    agent_name: str = "",
) -> None:
    """Bind module state. Called once at agent startup.

    Validates federal-tier URL allowlist policy and logs provider selection.
    """
    global _state
    cfg = WebConfig(**(config or {}))
    _enforce_tier_policy(cfg)
    _state = _State(
        config=cfg,
        telemetry=telemetry,
        workspace=workspace.resolve(),
        agent_name=agent_name,
    )
    _logger.info(
        "web: runtime configured tier=%s search=%s extract=%s allowlist_size=%d pii=%s",
        cfg.tier,
        cfg.search_provider,
        cfg.extract_provider,
        len(cfg.url_allowlist),
        cfg.pii_redaction_enabled,
    )


def state() -> _State:
    """Return the configured state. Raises if unconfigured."""
    if _state is None:
        raise RuntimeError(
            "web module called before runtime is configured; "
            "agent must call _runtime.configure(...) at startup"
        )
    return _state


def reset() -> None:
    """Test-only: clear runtime state."""
    global _state
    _state = None


async def get_search_provider() -> WebSearchProvider:
    """Return (and lazily build) the configured search provider."""
    st = state()
    if st.search_provider is None:
        st.search_provider = await _build_provider(st.config.search_provider, st.config)
    return st.search_provider


async def get_extract_provider() -> WebExtractProvider:
    """Return (and lazily build) the configured extract provider."""
    st = state()
    if st.extract_provider is None:
        st.extract_provider = await _build_provider(st.config.extract_provider, st.config)
    return st.extract_provider


# --- Provider construction ---------------------------------------------------


async def _build_provider(name: str, cfg: WebConfig) -> Any:
    """Resolve the API key and construct the named provider adapter.

    Returns Any: the concrete provider classes satisfy both the search and
    extract Protocols via duck-typing, so a single builder serves both.
    """
    api_key = await _resolve_api_key(name, cfg.tier)
    return _make_provider(name, api_key, cfg.request_timeout_s)


async def _resolve_api_key(provider_name: str, tier: str) -> str:
    """Resolve the provider API key via the tier-aware secret resolver.

    No vault backend is threaded into module runtimes, so resolution follows
    the resolver's env/file fallback: personal and enterprise honor the
    provider env var, while federal fails closed (VaultUnreachable) because
    a vault-backed secret is mandatory there and none is wired in.
    """
    env_var_map: dict[str, str] = {
        "parallel": "PARALLEL_API_KEY",
        "firecrawl": "FIRECRAWL_API_KEY",
        "tavily": "TAVILY_API_KEY",
    }
    secret_name = f"{provider_name}_api_key"
    env_var = env_var_map.get(provider_name)

    try:
        from arcagent.modules.vault.resolver import resolve_secret

        resolved: str = await resolve_secret(
            secret_name,
            tier=tier,
            backend=None,
            env_fallback_var=env_var,
        )
        return resolved
    except Exception as exc:  # reason: re-raise as a typed provider error
        from arcagent.modules.web.errors import ProviderConfigMissing

        raise ProviderConfigMissing(provider_name, secret_name) from exc


def _make_provider(name: str, api_key: str, timeout_s: float) -> Any:
    """Construct the named provider adapter."""
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


# --- Tier enforcement --------------------------------------------------------


def _enforce_tier_policy(cfg: WebConfig) -> None:
    """Raise RuntimeError if federal tier is configured without a URL allowlist.

    Federal deployments must declare explicit outbound destinations — an empty
    allowlist means deny-all, which renders the module inoperable (ASI04 +
    LLM06). We reject this at configure time rather than silently failing at
    first tool invocation.
    """
    if cfg.tier == "federal" and not cfg.url_allowlist:
        raise RuntimeError(
            "[federal] web module requires a non-empty url_allowlist. "
            "Configure [modules.web] url_allowlist in arcagent.toml."
        )


__all__ = [
    "configure",
    "get_extract_provider",
    "get_search_provider",
    "reset",
    "state",
]
