"""Per-run tool collection.

Built once from the caller's tool list and ``freeze()``-d by the loop before
the first turn. A frozen registry is immutable for the whole run: the tool set
stays byte-stable so the provider prompt-cache prefix (tools -> system ->
messages) is never invalidated mid-run, and no tool can be injected past the
point the caller fixed the set (ASI04 supply chain / LLM06 excessive agency).

arcrun knows about ordering and immutability, not caching — the provider
cache hit is an emergent benefit of a stable, deterministically-ordered list.
Dynamic capability belongs to a pre-run rebuild or a subagent with its own
registry, never a mid-run mutation of the live set.
"""

from __future__ import annotations

from arcllm.types import Tool as LLMTool

from arcrun.events import EventBus
from arcrun.types import Tool


class ToolRegistry:
    """Tool collection, mutable until frozen for the run."""

    def __init__(self, tools: list[Tool], event_bus: EventBus) -> None:
        self._tools: dict[str, Tool] = {t.name: t for t in tools}
        self._event_bus = event_bus
        self._frozen = False
        self._schema_cache: list[LLMTool] | None = None

    def freeze(self) -> None:
        """Seal the registry for the run. After this, add/remove raise."""
        self._frozen = True

    def add(self, tool: Tool) -> None:
        """Add or replace tool. Raises once frozen (mid-run mutation is denied)."""
        if self._frozen:
            self._event_bus.emit("tool.mutation_denied", {"name": tool.name, "op": "add"})
            raise RuntimeError(f"ToolRegistry is frozen for the run; cannot add {tool.name!r}")
        event_type = "tool.replaced" if tool.name in self._tools else "tool.registered"
        self._tools[tool.name] = tool
        self._schema_cache = None
        self._event_bus.emit(event_type, {"name": tool.name})

    def remove(self, name: str) -> None:
        """Remove tool by name. Raises once frozen; no-op if not found otherwise."""
        if self._frozen:
            self._event_bus.emit("tool.mutation_denied", {"name": name, "op": "remove"})
            raise RuntimeError(f"ToolRegistry is frozen for the run; cannot remove {name!r}")
        if name in self._tools:
            del self._tools[name]
            self._schema_cache = None
            self._event_bus.emit("tool.removed", {"name": name})

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def get_classification(self, name: str) -> str:
        """Return a tool's dispatch classification (SPEC-043 REQ-034).

        Consumed by ``parallel_dispatch.BatchClassifier`` to decide whether a
        turn's calls may run concurrently. Unknown tools are treated as
        ``state_modifying`` (fail-closed): the loop never parallelizes a call it
        cannot classify.
        """
        tool = self._tools.get(name)
        return tool.classification if tool is not None else "state_modifying"

    def list_schemas(self) -> list[LLMTool]:
        """Convert tools to arcllm Tool format for model.invoke().

        Memoized: the loop reads this every turn and the tool set is fixed
        once frozen, so the schema list is built once and reused (stable bytes
        for the cache prefix). Mutation before freeze invalidates the cache.
        """
        if self._schema_cache is None:
            self._schema_cache = [
                LLMTool(
                    name=t.name,
                    description=t.description,
                    parameters=t.input_schema,
                )
                for t in self._tools.values()
            ]
        return self._schema_cache

    def names(self) -> list[str]:
        return list(self._tools.keys())
