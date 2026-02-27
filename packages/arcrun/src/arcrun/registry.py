"""Dynamic mutable tool collection."""

from __future__ import annotations

from arcllm.types import Tool as LLMTool

from arcrun.events import EventBus
from arcrun.types import Tool


class ToolRegistry:
    """Mutable tool collection. Strategies read this each turn."""

    def __init__(self, tools: list[Tool], event_bus: EventBus) -> None:
        self._tools: dict[str, Tool] = {t.name: t for t in tools}
        self._event_bus = event_bus

    def add(self, tool: Tool) -> None:
        """Add or replace tool. Emits tool.registered (new) or tool.replaced (existing)."""
        event_type = "tool.replaced" if tool.name in self._tools else "tool.registered"
        self._tools[tool.name] = tool
        self._event_bus.emit(event_type, {"name": tool.name})

    def remove(self, name: str) -> None:
        """Remove tool by name. No-op if not found. Emits tool.removed if found."""
        if name in self._tools:
            del self._tools[name]
            self._event_bus.emit("tool.removed", {"name": name})

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def list_schemas(self) -> list[LLMTool]:
        """Convert tools to arcllm Tool format for model.invoke()."""
        return [
            LLMTool(
                name=t.name,
                description=t.description,
                parameters=t.input_schema,
            )
            for t in self._tools.values()
        ]

    def names(self) -> list[str]:
        return list(self._tools.keys())
