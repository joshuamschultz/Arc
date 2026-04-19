"""Sandbox policy enforcement for the browser module (T4.9).

Tier-driven rules:

  Federal   → sandbox=strict by default → remote browser required
  Enterprise → sandbox=loose default; strict optional
  Personal   → sandbox=loose; local headless OK

The single entry point is ``enforce_sandbox_policy()``. It takes the
tier name and the ``PlaywrightConfig``, validates the combination, and
raises ``LocalBrowserNotAllowed`` when the policy is violated.

This module contains NO I/O — it is pure policy logic so it is
trivially testable without any external dependencies.
"""

from __future__ import annotations

from typing import Literal

from arcagent.modules.browser.config import PlaywrightConfig
from arcagent.modules.browser.errors import LocalBrowserNotAllowed

# Tiers that default to strict-sandbox when no explicit sandbox is set.
_STRICT_BY_DEFAULT_TIERS: frozenset[str] = frozenset({"federal"})

# Tiers where strict sandbox is permitted but not the default.
_STRICT_OPTIONAL_TIERS: frozenset[str] = frozenset({"enterprise"})

# Tiers where strict is always loose-default; user may still override.
_LOOSE_DEFAULT_TIERS: frozenset[str] = frozenset({"personal"})


def effective_sandbox(
    tier: str,
    config: PlaywrightConfig,
) -> Literal["loose", "strict"]:
    """Return the effective sandbox mode for a given tier and config.

    Federal tier is always strict, regardless of what the config says.
    This prevents an operator from accidentally downgrading security by
    setting ``sandbox = "loose"`` in a federal deployment.

    Args:
        tier:   Deployment tier (``"federal"``, ``"enterprise"``, ``"personal"``).
        config: PlaywrightConfig for the current module instance.

    Returns:
        ``"strict"`` or ``"loose"`` effective sandbox mode.
    """
    if tier in _STRICT_BY_DEFAULT_TIERS:
        return "strict"
    return config.sandbox


def enforce_sandbox_policy(
    tier: str,
    config: PlaywrightConfig,
) -> None:
    """Raise ``LocalBrowserNotAllowed`` if the config violates sandbox policy.

    Call this before creating any browser session. The check is
    intentionally fail-loud: an operator that misconfigures a federal
    deployment gets an immediate, actionable error rather than a silent
    fallback.

    Policy matrix:
        ┌──────────┬────────────────────────────────────────────┐
        │ Tier     │ Enforcement                                │
        ├──────────┼────────────────────────────────────────────┤
        │ federal  │ Always strict; local mode → raise          │
        │ enterprise│ Strict if sandbox=strict; local → raise   │
        │ personal │ Only raises if sandbox=strict + mode=local  │
        └──────────┴────────────────────────────────────────────┘

    Args:
        tier:   Deployment tier.
        config: PlaywrightConfig for the current module instance.

    Raises:
        LocalBrowserNotAllowed: When local mode is configured in a
            sandbox that prohibits local headless browsers.
    """
    sandbox = effective_sandbox(tier, config)
    if sandbox == "strict" and config.mode == "local":
        raise LocalBrowserNotAllowed(
            tier=tier,
            details={
                "sandbox": sandbox,
                "mode": config.mode,
                "remote_provider": config.remote_provider,
                "remote_endpoint": config.remote_endpoint,
            },
        )
