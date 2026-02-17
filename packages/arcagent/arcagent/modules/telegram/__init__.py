"""Telegram messaging module — bidirectional human-agent interaction via Telegram Bot API."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arcagent.core.module_bus import ModuleBus, ModuleContext
from arcagent.core.telemetry import AgentTelemetry
from arcagent.modules.telegram.config import TelegramConfig

if TYPE_CHECKING:
    from arcagent.modules.telegram.bot import TelegramBot

_logger = logging.getLogger("arcagent.telegram")


class TelegramModule:
    """Telegram messaging module — Module Bus participant.

    Provides bidirectional text messaging between a human (Telegram)
    and ArcAgent. Supports inbound messages via long polling and
    proactive notifications via Module Bus event subscriptions.
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
        """Subscribe to events, create bot, and start polling loop."""
        from arcagent.modules.telegram.bot import TelegramBot

        self._bus = ctx.bus

        # Subscribe to lifecycle events
        ctx.bus.subscribe("agent:shutdown", self._on_agent_shutdown)

        # Subscribe to agent:ready for deferred binding of agent.chat().
        ctx.bus.subscribe("agent:ready", self._on_agent_ready)

        # Subscribe to schedule events for proactive notifications
        ctx.bus.subscribe("schedule:completed", self._on_schedule_completed)
        ctx.bus.subscribe("schedule:failed", self._on_schedule_failed)

        # Create and start bot
        self._bot = TelegramBot(
            config=self._config,
            telemetry=self._telemetry,
            workspace=self._workspace,
        )
        await self._bot.start()

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

    async def _on_schedule_completed(self, event: Any) -> None:
        """Handle schedule:completed — send agent response to user via Telegram."""
        if self._bot is None:
            return
        data = event.data if hasattr(event, "data") else {}
        result = data.get("result", "")
        schedule_name = data.get("schedule_name", "task")

        # Send the agent's response directly — it's already a natural-language reply.
        notification = result if result else f"Scheduled task completed: {schedule_name}"
        await self._bot.send_notification(notification)
        await self._emit_bus_event(
            "telegram:notification_forwarded",
            {
                "source_event": "schedule:completed",
                "schedule_name": schedule_name,
            },
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
