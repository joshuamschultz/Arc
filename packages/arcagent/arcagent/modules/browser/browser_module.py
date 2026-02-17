"""Browser Module — CDP-based browser interaction tools.

Implements the Module protocol. Manages CDP connection lifecycle
and registers browser tools with the ToolRegistry on startup.
Tools are standard RegisteredTools — ArcRun handles them like
any other tool with no special browser awareness.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from arcagent.core.module_bus import EventContext, ModuleContext
from arcagent.modules.browser.accessibility import AccessibilityManager
from arcagent.modules.browser.cdp_client import CDPClientManager
from arcagent.modules.browser.config import BrowserConfig
from arcagent.modules.browser.tools import create_browser_tools

_logger = logging.getLogger("arcagent.modules.browser")


class BrowserModule:
    """Module Bus subscriber providing CDP browser interaction tools.

    Implements the Module protocol. On startup:
    1. Connects to Chrome via CDP (launch or external endpoint)
    2. Creates AccessibilityManager for AX tree snapshots
    3. Registers browser tools with the ToolRegistry
    4. Subscribes to agent:shutdown for cleanup
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        workspace: Path = Path("."),
    ) -> None:
        self._config = BrowserConfig(**(config or {}))
        self._workspace = workspace.resolve()
        self._cdp: CDPClientManager | None = None
        self._ax: AccessibilityManager | None = None
        self._bus: Any = None

    @property
    def name(self) -> str:
        """Module name used in bus events and config keys."""
        return "browser"

    async def startup(self, ctx: ModuleContext) -> None:
        """Connect CDP, register tools, subscribe to events."""
        self._bus = ctx.bus
        self._cdp = CDPClientManager(self._config.connection)
        await self._cdp.connect()

        # Create accessibility manager for AX tree snapshots
        self._ax = AccessibilityManager(self._cdp, self._config)

        # Create and register all browser tools
        tools = create_browser_tools(
            cdp=self._cdp,
            ax=self._ax,
            config=self._config,
            bus=ctx.bus,
        )
        for tool in tools:
            ctx.tool_registry.register(tool)

        ctx.bus.subscribe("agent:shutdown", self._on_shutdown)
        await ctx.bus.emit(
            "browser.connected",
            {"cdp_url": self._cdp.url, "tool_count": len(tools)},
        )
        _logger.info(
            "Browser module started (cdp=%s, tools=%d)",
            self._cdp.url,
            len(tools),
        )

    async def shutdown(self) -> None:
        """Disconnect CDP and clean up Chrome process."""
        if self._cdp:
            await self._cdp.disconnect()
            self._cdp = None
        self._ax = None
        _logger.info("Browser module shut down")

    async def _on_shutdown(self, _ctx: EventContext) -> None:
        """Handle agent:shutdown event."""
        if self._bus is not None:
            await self._bus.emit("browser.disconnected", {})
        await self.shutdown()
