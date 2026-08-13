"""Per-agent web module runtime context.

The web module's tools share state — the resolved ``WebConfig``,
lazily-built provider clients, URL policy, and the telemetry sink for
audit events. Decorator-stamped functions can't carry that state in a
closure, so it lives in a :class:`_State` instance bound to a
:class:`contextvars.ContextVar`, configured by the agent at startup.

Task 27/32: a plain module global here is silently overwritten by
whichever agent's ``asyncio.Task`` most recently called ``configure()`` —
see ``arcagent/builtins/capabilities/_runtime.py`` for the full rationale.

Federal tier validation runs at :func:`configure` time so misconfiguration
is caught before any network request is attempted — fail fast, fail loud.

:func:`configure` also decides which of the module's two tools this deployment
may actually offer. A tool whose provider has no resolvable credential is
withheld from registration entirely (:func:`withheld_tools`): an agent must
never be advertised a capability it cannot use, because the model will call it,
the call will fail on a missing key, and the turn burns for nothing (LLM06,
LLM10). Every withholding is logged at WARNING with the tool, the provider and
the missing key — a capability that vanishes silently is a dead feature nobody
finds for months.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arcagent.modules.web.config import WebConfig
from arcagent.modules.web.protocols import WebExtractProvider, WebSearchProvider

_logger = logging.getLogger("arcagent.modules.web._runtime")

#: Provider name -> the environment variable its key is read from at personal
#: and enterprise tier. The canonical map: both the resolver call and the
#: withholding message name the key from here, so an operator is told the exact
#: variable to set.
_ENV_VAR_BY_PROVIDER: dict[str, str] = {
    "parallel": "PARALLEL_API_KEY",
    "firecrawl": "FIRECRAWL_API_KEY",
    "tavily": "TAVILY_API_KEY",
}

#: Providers that need no credential at all. ``browser`` reads pages through the
#: browser module's CDP backend, so basic site lookup works on a fresh box.
_KEYLESS_PROVIDERS = frozenset({"browser"})


@dataclass
class _State:
    """Mutable runtime state shared across web tools."""

    config: WebConfig
    telemetry: Any
    workspace: Path
    agent_name: str
    search_provider: WebSearchProvider | None = field(default=None)
    extract_provider: WebExtractProvider | None = field(default=None)
    #: Tool names this deployment must NOT register — decided once at
    #: :func:`configure` and read by ``capabilities.py`` as it is scanned.
    withheld: frozenset[str] = field(default_factory=frozenset)


_state_var: contextvars.ContextVar[_State | None] = contextvars.ContextVar(
    "arcagent_web_state", default=None
)


def configure(
    *,
    config: dict[str, Any] | None = None,
    telemetry: Any = None,
    workspace: Path = Path("."),
    agent_name: str = "",
) -> None:
    """Bind module state for the CURRENT asyncio task. Called once at agent startup.

    Validates federal-tier URL allowlist policy and logs provider selection.
    """
    cfg = WebConfig(**(config or {}))
    _enforce_tier_policy(cfg)
    _state_var.set(
        _State(
            config=cfg,
            telemetry=telemetry,
            workspace=workspace.resolve(),
            agent_name=agent_name,
            withheld=_decide_withheld(cfg),
        )
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
    current = _state_var.get()
    if current is None:
        raise RuntimeError(
            "web module called before runtime is configured; "
            "agent must call _runtime.configure(...) at startup"
        )
    return current


def bind(state_obj: _State) -> None:
    """Idempotently bind an already-built ``_State`` into the CURRENT task.

    Cheap — one ``.set()`` call, no construction. Called at the top of
    every turn-dispatch entry point (task 27 follow-up hotfix) so a turn
    running in a fresh sibling ``asyncio.Task`` — not a descendant of the
    task that ran ``configure()`` — still sees this agent's state.
    """
    _state_var.set(state_obj)


def reset() -> None:
    """Test-only: clear runtime state."""
    _state_var.set(None)


def withheld_tools() -> frozenset[str]:
    """Tool names this deployment may not register, decided at :func:`configure`.

    Empty when the runtime was never configured: importing ``capabilities.py``
    as a plain library (a test, a docs build, the arcui capability inventory)
    must not turn into a startup failure, and nothing can be dispatched from
    there anyway.
    """
    current = _state_var.get()
    return frozenset() if current is None else current.withheld


async def get_search_provider() -> WebSearchProvider:
    """Return (and lazily build) the configured search provider."""
    st = state()
    if st.search_provider is None:
        name = st.config.search_provider
        if name is None:
            from arcagent.modules.web.errors import ProviderConfigMissing

            raise ProviderConfigMissing("web_search", "search_provider")
        st.search_provider = await _build_provider(name, st.config)
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
    if name in _KEYLESS_PROVIDERS:
        return _make_keyless_provider(name, cfg)
    api_key = await _resolve_api_key(name, cfg.tier)
    return _make_provider(name, api_key, cfg.request_timeout_s)


async def _resolve_api_key(provider_name: str, tier: str) -> str:
    """Resolve the provider API key via the tier-aware secret resolver.

    No vault backend is threaded into module runtimes, so resolution follows
    the resolver's env/file fallback: personal and enterprise honor the
    provider env var, while federal fails closed (VaultUnreachable) because
    a vault-backed secret is mandatory there and none is wired in.
    """
    secret_name = f"{provider_name}_api_key"
    env_var = _ENV_VAR_BY_PROVIDER.get(provider_name)

    try:
        from arcagent.core.vault.resolver import resolve_secret

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


def _make_keyless_provider(name: str, cfg: WebConfig) -> Any:
    """Construct a provider that needs no credential."""
    from arcagent.modules.web.providers.browser_page import BrowserPageProvider

    if name != "browser":
        raise ValueError(f"Unknown keyless web provider: {name!r}")
    return BrowserPageProvider.create(
        cdp_url=cfg.browser_cdp_url, tier=cfg.tier, timeout_s=cfg.request_timeout_s
    )


# --- Tool availability -------------------------------------------------------


def _decide_withheld(cfg: WebConfig) -> frozenset[str]:
    """Return the tools this deployment must not register, logging each reason.

    Runs once per agent at startup. Both checks are construction-only probes —
    a credential lookup and a backend build — so nothing here opens a socket or
    launches a process.
    """
    withheld: set[str] = set()

    search_reason = _unavailable_reason(cfg.search_provider, cfg)
    if search_reason is not None:
        withheld.add("web_search")
        _logger.warning(
            "web: NOT registering web_search — %s. "
            "Set [modules.web.config] search_provider and its API key to enable it.",
            search_reason,
        )

    extract_reason = _unavailable_reason(cfg.extract_provider, cfg)
    if extract_reason is not None:
        withheld.add("web_extract")
        _logger.warning(
            "web: NOT registering web_extract — %s. "
            "The default provider 'browser' needs no key; "
            "a paid provider needs its API key.",
            extract_reason,
        )

    return frozenset(withheld)


def _unavailable_reason(provider: str | None, cfg: WebConfig) -> str | None:
    """``None`` when ``provider`` can run here, else why it cannot, for a log line."""
    if provider is None:
        return "no provider is configured"
    if provider in _KEYLESS_PROVIDERS:
        return _keyless_unavailable_reason(provider, cfg)
    env_var = _ENV_VAR_BY_PROVIDER.get(provider, "<unknown>")
    if _run_sync(_resolve_api_key(provider, cfg.tier)) is None:
        return (
            f"provider {provider!r} has no resolvable API key "
            f"(secret {provider}_api_key / env {env_var}, tier={cfg.tier})"
        )
    return None


def _keyless_unavailable_reason(provider: str, cfg: WebConfig) -> str | None:
    """``None`` when the keyless provider's backing seam is present and permitted."""
    from arcagent.modules.web.providers.browser_page import build_browser_backend

    try:
        build_browser_backend(cdp_url=cfg.browser_cdp_url, tier=cfg.tier)
    except Exception as exc:
        return f"provider {provider!r} is unusable: {type(exc).__name__}: {exc}"
    return None


def _run_sync(coro: Coroutine[Any, Any, str]) -> str | None:
    """Drive a secret-resolution coroutine to completion from sync code.

    ``configure()`` is called synchronously by ``configure_module_runtimes``
    while the agent's event loop is already running, and the tier-aware
    resolver — the one credential path we may use — is async. So the coroutine
    runs on its own loop in a worker thread. This is a startup probe of at most
    two providers, never a per-request path.

    Returns the secret, or ``None`` when it does not resolve.
    """
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="arc-web-keyprobe") as pool:
        try:
            return pool.submit(asyncio.run, coro).result()
        except Exception:  # reason: any resolution failure means "no key", not a crash
            return None


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
    "bind",
    "configure",
    "get_extract_provider",
    "get_search_provider",
    "reset",
    "state",
    "withheld_tools",
]
