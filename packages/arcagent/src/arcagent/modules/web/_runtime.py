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

    Validates federal-tier URL allowlist policy and logs provider selection,
    matching legacy :class:`WebModule.__init__` behaviour.
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
    "reset",
    "state",
]
