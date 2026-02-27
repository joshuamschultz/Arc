"""Permission boundary. Allowlist model. Deny-by-default when configured."""

from __future__ import annotations

from typing import Any

from arcrun.events import EventBus
from arcrun.types import SandboxConfig


class Sandbox:
    """Checks permissions before every tool execution."""

    def __init__(self, config: SandboxConfig | None, event_bus: EventBus) -> None:
        self._config = config
        self._event_bus = event_bus

    def _deny(self, tool_name: str, reason: str) -> tuple[bool, str]:
        """Emit denial event and return (False, reason)."""
        self._event_bus.emit("tool.denied", {"name": tool_name, "reason": reason})
        return False, reason

    async def check(self, tool_name: str, params: dict[str, Any]) -> tuple[bool, str]:
        """
        Returns (allowed, reason).

        Check order:
        1. No config -> (True, "")
        2. allowed_tools set and tool not in list -> (False, "not in allowed tools")
        3. check callback provided -> delegate to callback
        4. All checks pass -> (True, "")

        Emits tool.denied for denials.
        check() callback exceptions -> treated as denial (fail-safe).
        """
        if self._config is None:
            return True, ""

        if self._config.allowed_tools is not None and tool_name not in self._config.allowed_tools:
            return self._deny(tool_name, f"{tool_name}: not in allowed tools")

        if self._config.check is not None:
            try:
                allowed, reason = await self._config.check(tool_name, params)
            except Exception:
                return self._deny(tool_name, "check callback error")
            if not allowed:
                return self._deny(tool_name, reason)

        return True, ""
