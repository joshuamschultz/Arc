"""Proactive module configuration (SPEC-017).

Only the leader-election selector and its backend parameters are configurable;
the engine's tick cadence is fixed by the capability. ``extra='forbid'`` (via
:class:`ModuleConfig`) turns a misspelled key into a loud error rather than a
silent single-instance fallback that would violate R-048.
"""

from __future__ import annotations

from arcagent.core.module_config import ModuleConfig


class ProactiveConfig(ModuleConfig):
    """Configuration for the proactive module."""

    enabled: bool = True
    # Leader-election backend: 'noop' (default, single-instance), 'redis', 'k8s'.
    leader: str = "noop"
    # Election identity; defaults (at build time) to agent name / hostname.
    identity: str | None = None
    # Redis backend (leader='redis').
    redis_url: str | None = None
    redis_key: str = "arcagent:proactive:leader"
    # Kubernetes Lease backend (leader='k8s').
    k8s_namespace: str | None = None
    k8s_lease_name: str | None = None
