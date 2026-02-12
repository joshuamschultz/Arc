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
            reason = f"{tool_name}: not in allowed tools"
            self._event_bus.emit(
                "tool.denied", {"name": tool_name, "reason": reason}
            )
            return False, reason

        if self._config.check is not None:
            try:
                allowed, reason = await self._config.check(tool_name, params)
            except Exception:
                reason = "check callback error"
                self._event_bus.emit(
                    "tool.denied", {"name": tool_name, "reason": reason}
                )
                return False, reason
            if not allowed:
                self._event_bus.emit(
                    "tool.denied", {"name": tool_name, "reason": reason}
                )
                return False, reason

        return True, ""
