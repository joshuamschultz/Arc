"""Telegram messaging module — bidirectional human-agent interaction via Telegram Bot API."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arcagent.core.module_bus import ModuleBus, ModuleContext
from arcagent.core.telemetry import AgentTelemetry
from arcagent.core.tool_registry import RegisteredTool, ToolTransport
from arcagent.modules.telegram.config import TelegramConfig

if TYPE_CHECKING:
    from arcagent.modules.telegram.bot import TelegramBot

_logger = logging.getLogger("arcagent.telegram")


class TelegramModule:
    """Telegram messaging module — Module Bus participant.

    Provides bidirectional text messaging between a human (Telegram)
    and ArcAgent. Supports inbound messages via long polling and
    proactive notifications via an LLM-callable tool. The agent
    decides what's worth sending — no auto-forwarding of schedule results.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        telemetry: AgentTelemetry | None = None,
        workspace: Path = Path("."),
    ) -> None:
        self._config = TelegramConfig(**(config or {}))
        self._telemetry = telemetry
        self._workspace = workspace
        self._bot: TelegramBot | None = None
        self._bus: ModuleBus | None = None

    @property
    def name(self) -> str:
        return "telegram"

    async def startup(self, ctx: ModuleContext) -> None:
        """Subscribe to events, create bot, register tools, and start polling."""
        from arcagent.modules.telegram.bot import TelegramBot

        self._bus = ctx.bus

        # Subscribe to lifecycle events
        ctx.bus.subscribe("agent:shutdown", self._on_agent_shutdown)

        # Subscribe to agent:ready for deferred binding of agent.chat().
        ctx.bus.subscribe("agent:ready", self._on_agent_ready)

        # Subscribe to schedule failure notifications (failures are always worth knowing).
        ctx.bus.subscribe("schedule:failed", self._on_schedule_failed)

        # Create and start bot
        self._bot = TelegramBot(
            config=self._config,
            telemetry=self._telemetry,
            workspace=self._workspace,
        )
        await self._bot.start()

        # Register notify_user tool — agent decides what's worth sending.
        ctx.tool_registry.register(self._create_notify_tool())

        # Emit Module Bus event for observability
        await self._emit_bus_event("telegram:module_started", {})
        _logger.info("Telegram module started")

    async def shutdown(self) -> None:
        """Stop polling loop and clean up. Safe to call multiple times."""
        if self._bot is not None:
            await self._bot.stop()
            self._bot = None
        await self._emit_bus_event("telegram:module_stopped", {})
        _logger.info("Telegram module stopped")

    def set_agent_chat_fn(self, fn: Callable[..., Awaitable[Any]]) -> None:
        """Bind the agent.chat() callback after startup (deferred binding)."""
        if self._bot is not None:
            self._bot.set_agent_chat_fn(fn)

    async def _on_agent_ready(self, event: Any) -> None:
        """Handle agent:ready — bind agent.chat() callback."""
        data = event.data if hasattr(event, "data") else {}
        chat_fn = data.get("chat_fn")
        if chat_fn is not None:
            self.set_agent_chat_fn(chat_fn)
            _logger.info("Bound agent_chat_fn via agent:ready event")

    async def _on_agent_shutdown(self, event: Any) -> None:
        """Handle agent:shutdown event."""
        await self.shutdown()

    def _create_notify_tool(self) -> RegisteredTool:
        """Create the notify_user tool — agent calls this to message the human."""
        bot = self._bot

        async def _handle_notify_user(
            message: str = "",
            **kwargs: Any,
        ) -> str:
            """Send a notification to the user via Telegram."""
            if not message:
                return json.dumps({"error": "message is required"})
            if bot is None:
                return json.dumps({"error": "Telegram bot not running"})
            await bot.send_notification(message)
            _logger.info("Agent sent user notification (%d chars)", len(message))
            return json.dumps({"status": "sent", "length": len(message)})

        return RegisteredTool(
            name="notify_user",
            description=(
                "Send a message to the user via Telegram. Use this ONLY when "
                "you have a meaningful update, result, question, or need "
                "direction. Do NOT use for routine status like 'no new messages' "
                "or 'task completed with no findings'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "The message to send to the user",
                    },
                },
                "required": ["message"],
            },
            transport=ToolTransport.NATIVE,
            execute=_handle_notify_user,
            timeout_seconds=30,
            source="telegram",
        )

    async def _on_schedule_failed(self, event: Any) -> None:
        """Handle schedule:failed — notify user of failure."""
        if self._bot is None:
            return
        data = event.data if hasattr(event, "data") else {}
        error = data.get("error", "unknown error")
        schedule_name = data.get("schedule_name", "task")
        await self._bot.send_notification(f"Scheduled task failed: {error}")
        await self._emit_bus_event(
            "telegram:notification_forwarded",
            {
                "source_event": "schedule:failed",
                "schedule_name": schedule_name,
            },
        )

    async def _emit_bus_event(self, event_name: str, data: dict[str, Any]) -> None:
        """Emit an event on the Module Bus if available."""
        if self._bus is not None:
            await self._bus.emit(event_name, data)


__all__ = ["TelegramModule"]
