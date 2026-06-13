"""Generic adapter-plugin registry — gateway core's platform-agnostic loader.

The gateway core contains **zero** platform-specific code. Each chat platform
(Telegram, Slack, Mattermost, …) ships as a separately-installed extension
package that registers an :class:`AdapterPlugin` under the
``arcgateway.adapters`` entry-point group::

    # arcgateway-telegram/pyproject.toml
    [project.entry-points."arcgateway.adapters"]
    telegram = "arcgateway_telegram:PLUGIN"

At startup the gateway discovers every registered plugin, applies the
four-pillar **Authorize**/**Audit** gate, and builds an adapter for each
``[platforms.<name>]`` block that is ``enabled = true``.

Four Pillars (ADR-019) — tier is *stringency metadata, not a gate*:

    Identity   Each adapter carries the agent DID it serves (ctx.agent_did()).
    Sign       Entry points are only registrable by installed distributions;
               an allowlist of official plugin names is the load-time control
               point where Sigstore/arctrust verification will attach.
    Authorize  Plugin names are regex-validated (no path traversal / injection,
               ASI04 / NIST SI-10). Unofficial plugins load at personal/
               enterprise with an audit warning (self-signed posture) but are
               **blocked** at federal (signed-allowlist requirement).
    Audit      Every load / skip / block emits a ``gateway.adapter.*`` event.

Credential-presence gating (NanoClaw's lesson): a plugin whose credentials or
optional dependency are missing raises :class:`AdapterUnavailableError` (or
``ImportError``); the registry skips it at personal/enterprise but treats it as
a hard startup failure at federal — a federal deployment that enabled an
adapter must refuse to start rather than serve a subset silently.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any, Protocol

from arcgateway.audit import emit_event

if TYPE_CHECKING:
    from arcgateway.adapters.base import BasePlatformAdapter
    from arcgateway.executor import InboundEvent

_logger = logging.getLogger("arcgateway.adapters.registry")

#: Entry-point group extension packages register their plugin under.
ENTRY_POINT_GROUP = "arcgateway.adapters"

# Platform names: lowercase, start with a letter, max 32 chars. Mirrors
# arcllm's provider-name guard — blocks "../evil", "os.system", etc.
_VALID_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

#: First-party adapter plugins → their expected distribution package name.
#: At federal tier only these names may load (signed-allowlist control point).
OFFICIAL_ADAPTERS: dict[str, str] = {
    "telegram": "arcgateway-telegram",
    "slack": "arcgateway-slack",
    "mattermost": "arcgateway-mattermost",
}

OnMessage = Callable[["InboundEvent"], Awaitable[None]]


class AdapterUnavailableError(Exception):
    """A plugin could not build its adapter (missing credentials/config).

    Plugins raise this (or ``ImportError`` for a missing optional dependency)
    to signal a skippable condition. The registry skips the adapter at
    personal/enterprise tier and re-raises it at federal tier.
    """


def validate_adapter_name(name: str) -> None:
    """Reject platform names that could enable path traversal or injection.

    Args:
        name: Platform name from a ``[platforms.<name>]`` block or entry point.

    Raises:
        ValueError: If ``name`` does not match ``[a-z][a-z0-9_]{0,31}``.
    """
    if not _VALID_NAME_RE.match(name):
        msg = f"invalid adapter name {name!r}; must match [a-z][a-z0-9_]{{0,31}}"
        raise ValueError(msg)


@dataclass(frozen=True)
class AdapterBuildContext:
    """Everything a plugin needs to construct its adapter.

    Attributes:
        name: The platform name (``[platforms.<name>]`` key).
        raw_config: The raw TOML block for this platform, including ``enabled``
            and an optional ``agent_did`` override. The plugin validates this
            against its own Pydantic model.
        on_message: Async callback the adapter calls with each inbound event
            (wired to ``SessionRouter.handle`` in production).
        default_agent_did: The gateway-level ``[gateway].agent_did``.
        tier: Deployment tier — ``personal`` | ``enterprise`` | ``federal``.
    """

    name: str
    raw_config: dict[str, Any]
    on_message: OnMessage
    default_agent_did: str
    tier: str

    def agent_did(self) -> str:
        """Return the DID this adapter serves — block override or gateway default."""
        override = self.raw_config.get("agent_did")
        if isinstance(override, str) and override:
            return override
        return self.default_agent_did


class AdapterBuilder(Protocol):
    """Builds an adapter from a context, or raises if it cannot."""

    def __call__(self, ctx: AdapterBuildContext) -> BasePlatformAdapter: ...


@dataclass(frozen=True)
class AdapterPlugin:
    """A platform adapter plugin exported by an extension package.

    Extension packages expose a module-level ``PLUGIN = AdapterPlugin(...)`` and
    register it under the ``arcgateway.adapters`` entry-point group.

    Attributes:
        name: Platform name (must satisfy :func:`validate_adapter_name`).
        build: Callable that validates ``ctx.raw_config``, resolves credentials,
            and returns a connected-on-``connect()`` adapter — or raises
            :class:`AdapterUnavailableError` / ``ImportError`` when it cannot build.
    """

    name: str
    build: AdapterBuilder


def discover_plugins() -> dict[str, AdapterPlugin]:
    """Discover installed adapter plugins via entry points.

    Iterating entry points is side-effect-free; each plugin object is loaded
    (its module imported) only here, once. Malformed or wrong-typed entries are
    audited and skipped rather than crashing discovery.

    Returns:
        Mapping of validated platform name → :class:`AdapterPlugin`.
    """
    found: dict[str, AdapterPlugin] = {}
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            validate_adapter_name(ep.name)
        except ValueError:
            _audit("gateway.adapter.blocked", ep.name, "deny", reason="invalid_name")
            _logger.warning("registry: blocked entry point with invalid name %r", ep.name)
            continue
        try:
            plugin = ep.load()
        except Exception:  # reason: a broken plugin must not abort discovery
            _audit("gateway.adapter.blocked", ep.name, "error", reason="load_failed")
            _logger.exception("registry: failed to load adapter plugin %r", ep.name)
            continue
        if not isinstance(plugin, AdapterPlugin):
            _audit("gateway.adapter.blocked", ep.name, "deny", reason="not_a_plugin")
            _logger.warning("registry: entry point %r did not export an AdapterPlugin", ep.name)
            continue
        found[ep.name] = plugin
    return found


def build_adapters(
    *,
    platforms: dict[str, dict[str, Any]],
    on_message: OnMessage,
    default_agent_did: str,
    tier: str,
    plugins: dict[str, AdapterPlugin] | None = None,
) -> list[BasePlatformAdapter]:
    """Build adapters for every enabled platform block, generically.

    Args:
        platforms: ``{name: raw_block}`` for each ``[platforms.<name>]`` other
            than the core ``web`` adapter.
        on_message: Inbound callback wired to ``SessionRouter.handle``.
        default_agent_did: ``[gateway].agent_did``.
        tier: ``personal`` | ``enterprise`` | ``federal``.
        plugins: Pre-discovered plugins (defaults to :func:`discover_plugins`).
            Injected directly in tests.

    Returns:
        Adapters for each enabled, authorized, buildable platform.

    Raises:
        AdapterUnavailableError: At federal tier, when an enabled platform cannot be
            loaded (plugin missing, unofficial, or its build failed) — federal
            fails closed rather than serving a silent subset.
    """
    plugins = discover_plugins() if plugins is None else plugins
    is_federal = tier == "federal"
    adapters: list[BasePlatformAdapter] = []

    for name, block in platforms.items():
        if not isinstance(block, dict) or not block.get("enabled"):
            continue

        try:
            validate_adapter_name(name)
        except ValueError:
            _audit("gateway.adapter.blocked", name, "deny", reason="invalid_name")
            _logger.warning("registry: skipping platform with invalid name %r", name)
            if is_federal:
                raise AdapterUnavailableError(f"invalid adapter name {name!r}") from None
            continue

        if name not in OFFICIAL_ADAPTERS:
            # Unofficial plugin: blocked at federal, allowed-with-warning otherwise.
            if is_federal:
                _audit("gateway.adapter.blocked", name, "deny", reason="not_official")
                raise AdapterUnavailableError(f"adapter {name!r} is not in the federal allowlist")
            _audit("gateway.adapter.unverified", name, "allow", reason="not_official")
            _logger.warning(
                "registry: loading unofficial adapter %r (personal/enterprise only)", name
            )

        plugin = plugins.get(name)
        if plugin is None:
            _audit("gateway.adapter.skipped", name, "deny", reason="not_installed")
            msg = (
                f"adapter {name!r} enabled but its plugin package is not installed "
                f"(expected: {OFFICIAL_ADAPTERS.get(name, 'an arcgateway adapter package')})"
            )
            if is_federal:
                raise AdapterUnavailableError(msg)
            _logger.warning("registry: %s — skipping", msg)
            continue

        ctx = AdapterBuildContext(
            name=name,
            raw_config=block,
            on_message=on_message,
            default_agent_did=default_agent_did,
            tier=tier,
        )
        try:
            adapter = plugin.build(ctx)
        except (AdapterUnavailableError, ImportError) as exc:
            _audit("gateway.adapter.skipped", name, "deny", reason=type(exc).__name__)
            if is_federal:
                raise AdapterUnavailableError(f"adapter {name!r} unavailable: {exc}") from exc
            _logger.warning("registry: adapter %r unavailable — skipping: %s", name, exc)
            continue

        _audit(
            "gateway.adapter.loaded",
            name,
            "allow",
            reason="official" if name in OFFICIAL_ADAPTERS else "unofficial",
        )
        _logger.info("registry: loaded adapter %r (agent_did=%s)", name, ctx.agent_did())
        adapters.append(adapter)

    return adapters


def _audit(action: str, name: str, outcome: str, *, reason: str) -> None:
    """Emit a registry audit event (swallowed by the audit layer per AU-5)."""
    emit_event(
        action=action,
        target=f"adapter:{name}",
        outcome=outcome,
        extra={"adapter": name, "reason": reason},
    )


__all__ = [
    "ENTRY_POINT_GROUP",
    "OFFICIAL_ADAPTERS",
    "AdapterBuildContext",
    "AdapterBuilder",
    "AdapterPlugin",
    "AdapterUnavailableError",
    "build_adapters",
    "discover_plugins",
    "validate_adapter_name",
]
