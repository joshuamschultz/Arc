"""Planning module — personal task notebook for multi-step work.

Like a notebook on your desk: track what you're working on,
what's pending, and what's done. Tasks persist across turns
in tasks.json. No team coordination — just your own todo list.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from arcagent.core.module_bus import EventContext, ModuleContext
from arcagent.modules.base_config import ModuleConfig

_logger = logging.getLogger("arcagent.planning")


class PlanningConfig(ModuleConfig):
    """Planning module configuration."""

    enabled: bool = False


class PlanningModule:
    """Personal task notebook — Module Bus participant.

    On startup:
    1. Registers 4 LLM-callable tools (create, list, update, complete)
    2. Subscribes to agent:assemble_prompt to surface pending tasks
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        workspace: Path = Path("."),
        **_kw: Any,
    ) -> None:
        self._config = PlanningConfig(**(config or {}))
        self._workspace = workspace

    @property
    def name(self) -> str:
        return "planning"

    async def startup(self, ctx: ModuleContext) -> None:
        """Register tools and subscribe to prompt assembly."""
        from arcagent.modules.planning.tools import create_planning_tools

        tools = create_planning_tools(self._workspace)
        for tool in tools:
            ctx.tool_registry.register(tool)

        ctx.bus.subscribe(
            "agent:assemble_prompt",
            self._on_assemble_prompt,
            priority=60,
        )
        ctx.bus.subscribe("agent:shutdown", self._on_agent_shutdown)

        _logger.info("Planning module started")

    async def shutdown(self) -> None:
        """No background tasks to clean up."""
        _logger.info("Planning module stopped")

    async def _on_agent_shutdown(self, event: Any) -> None:
        """Handle agent:shutdown event."""
        await self.shutdown()

    def _load_pending_tasks(self) -> list[dict[str, Any]]:
        """Read pending tasks from workspace. Returns incomplete tasks."""
        tasks_path = self._workspace / "tasks.json"
        if not tasks_path.exists():
            return []
        try:
            data = json.loads(tasks_path.read_text(encoding="utf-8"))
            return [t for t in data if t.get("status") != "done"]
        except (json.JSONDecodeError, OSError):
            return []

    async def _on_assemble_prompt(self, ctx: EventContext) -> None:
        """Inject pending tasks into the system prompt."""
        sections = ctx.data.get("sections", {})

        pending = self._load_pending_tasks()
        if not pending:
            return

        lines = [
            f"## Pending Tasks ({len(pending)} incomplete)",
            "",
        ]
        for task in pending:
            status = task.get("status", "pending")
            lines.append(
                f"- **[{status}]** `{task.get('id', '?')}`: "
                f"{task.get('description', '(no description)')}"
            )

        sections["planning"] = "\n".join(lines)


__all__ = ["PlanningModule"]
