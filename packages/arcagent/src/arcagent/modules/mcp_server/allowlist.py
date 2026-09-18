"""SPEC-082 COMP-005 / REQ-415 — the door's explicit, tier-aware exposure allowlist.

The door exposes only tool verbs an operator named. ``tools/list`` returns the
allowlist intersected with the real catalog; a ``tools/call`` on an unlisted verb
is refused at this layer, before any dispatch object exists.

Tier is stringency, not a gate: at enterprise/federal an unbounded (``*``) or empty
allowlist is refused outright (D-548, ASI02/LLM06); at personal ``*`` is permitted.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from arcagent.modules.mcp_server.config import McpServerConfig

#: Tiers where an unbounded or empty allowlist is refused.
_STRICT_TIERS = frozenset({"enterprise", "federal"})

#: The wildcard token that opens the full catalog (personal tier only).
_WILDCARD = "*"


class AllowlistRefused(Exception):  # noqa: N818 — name is the door's public contract (tests import it)
    """Raised for an unbounded/empty allowlist outside personal, or an unlisted call."""


@dataclass(frozen=True)
class ExposureAllowlist:
    """The set of verbs a door may expose, resolved against its tier."""

    allowed: frozenset[str]
    wildcard: bool

    @classmethod
    def from_config(cls, config: McpServerConfig, tier: str) -> ExposureAllowlist:
        """Build a tier-checked allowlist from ``[modules.mcp_server].expose``.

        Fails closed at enterprise/federal on an unbounded or empty allowlist —
        the door must name the tools it opens.
        """
        expose = list(config.expose)
        wildcard = _WILDCARD in expose
        if tier in _STRICT_TIERS and (wildcard or not expose):
            raise AllowlistRefused(
                f"tier {tier!r} refuses an unbounded ('*') or empty MCP exposure "
                "allowlist; name each exposed verb explicitly (REQ-415)"
            )
        allowed = frozenset(name for name in expose if name != _WILDCARD)
        return cls(allowed=allowed, wildcard=wildcard)

    def is_allowed(self, name: str) -> bool:
        """Whether ``name`` may be exposed/called under this allowlist."""
        return self.wildcard or name in self.allowed

    def filter(self, names: Iterable[str]) -> list[str]:
        """The subset of ``names`` this allowlist exposes — allowlist ∩ catalog."""
        return [name for name in names if self.is_allowed(name)]

    def check_call(self, name: str) -> None:
        """Raise :class:`AllowlistRefused` if ``name`` is not exposed — a pure gate."""
        if not self.is_allowed(name):
            raise AllowlistRefused(f"tool {name!r} is not on the door's exposure allowlist")


__all__ = ["AllowlistRefused", "ExposureAllowlist"]
