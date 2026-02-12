"""Tests for dynamic tool registry."""
import pytest

from arcrun.events import EventBus
from arcrun.types import Tool


async def _noop(params: dict, ctx: object) -> str:
    return "ok"


def _make_tool(name: str = "t1") -> Tool:
    return Tool(name=name, description=f"Tool {name}", input_schema={"type": "object"}, execute=_noop)


class TestToolRegistry:
    def _make_bus(self) -> EventBus:
        return EventBus(run_id="test")

    def test_init_from_tool_list(self):
        from arcrun.registry import ToolRegistry

        bus = self._make_bus()
        reg = ToolRegistry(tools=[_make_tool("a"), _make_tool("b")], event_bus=bus)
        assert reg.names() == ["a", "b"]

    def test_add_tool(self):
        from arcrun.registry import ToolRegistry

        bus = self._make_bus()
        reg = ToolRegistry(tools=[], event_bus=bus)
        reg.add(_make_tool("new"))
        assert reg.get("new") is not None
        assert "new" in reg.names()

    def test_remove_tool(self):
        from arcrun.registry import ToolRegistry

        bus = self._make_bus()
        reg = ToolRegistry(tools=[_make_tool("x")], event_bus=bus)
        reg.remove("x")
        assert reg.get("x") is None
        assert "x" not in reg.names()

    def test_remove_nonexistent_is_noop(self):
        from arcrun.registry import ToolRegistry

        bus = self._make_bus()
        reg = ToolRegistry(tools=[], event_bus=bus)
        reg.remove("ghost")  # should not raise

    def test_replace_duplicate_name(self):
        from arcrun.registry import ToolRegistry

        bus = self._make_bus()
        t1 = _make_tool("dup")
        t2 = Tool(name="dup", description="replaced", input_schema={}, execute=_noop)
        reg = ToolRegistry(tools=[t1], event_bus=bus)
        reg.add(t2)
        assert reg.get("dup").description == "replaced"
        assert reg.names().count("dup") == 1

    def test_get_by_name(self):
        from arcrun.registry import ToolRegistry

        bus = self._make_bus()
        tool = _make_tool("target")
        reg = ToolRegistry(tools=[tool], event_bus=bus)
        assert reg.get("target") is tool
        assert reg.get("missing") is None

    def test_list_schemas(self):
        from arcrun.registry import ToolRegistry

        bus = self._make_bus()
        tool = _make_tool("s")
        reg = ToolRegistry(tools=[tool], event_bus=bus)
        schemas = reg.list_schemas()
        assert len(schemas) == 1
        assert schemas[0]["name"] == "s"
        assert schemas[0]["description"] == "Tool s"
        assert "input_schema" in schemas[0]

    def test_add_emits_registered_event(self):
        from arcrun.registry import ToolRegistry

        bus = self._make_bus()
        reg = ToolRegistry(tools=[], event_bus=bus)
        reg.add(_make_tool("new"))
        events = [e for e in bus.events if e.type == "tool.registered"]
        assert len(events) == 1
        assert events[0].data["name"] == "new"

    def test_remove_emits_removed_event(self):
        from arcrun.registry import ToolRegistry

        bus = self._make_bus()
        reg = ToolRegistry(tools=[_make_tool("gone")], event_bus=bus)
        reg.remove("gone")
        events = [e for e in bus.events if e.type == "tool.removed"]
        assert len(events) == 1
        assert events[0].data["name"] == "gone"
