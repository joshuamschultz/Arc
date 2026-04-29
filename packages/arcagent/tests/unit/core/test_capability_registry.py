"""SPEC-021 Task 1.5 — CapabilityRegistry.

Five kind-discriminated dicts (tools/skills/hooks/tasks/capabilities)
guarded by an aiorwlock. Tool calls + ``format_for_prompt`` take
reader locks; ``register_*`` / ``unregister`` take writer locks. The
manifest XML is cached and invalidated on every mutation.

What 1.5 covers (excluded from later tasks):
  * round-trip register → get_* for each kind
  * conflict resolution per kind (R-004):
      - tools/skills last-wins
      - hooks fan-out (event-keyed list, priority-sorted)
      - background_task drain-then-replace
  * manifest XML format
  * cache invalidated on mutation
  * concurrent readers + exclusive writer
  * ``to_arcrun_tools`` emits arcrun.Tool entries

Bus event audit emission is task 1.10; basic outcome is verified here
via in-memory event sink.
"""

from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from arcagent.tools._decorator import (
    BackgroundTaskMetadata,
    CapabilityClassMetadata,
    HookMetadata,
    ToolMetadata,
)


def _tool_meta(name: str = "echo", **overrides: object) -> ToolMetadata:
    base = {
        "name": name,
        "description": f"{name} desc",
        "input_schema": {"type": "object", "properties": {}},
        "classification": "read_only",
        "capability_tags": (),
        "when_to_use": f"use {name}",
        "version": "1.0.0",
    }
    base.update(overrides)
    return ToolMetadata(**base)  # type: ignore[arg-type]


async def _noop(**_: object) -> str:
    return "ok"


@pytest.mark.asyncio
class TestRegisterTool:
    async def test_register_then_get(self) -> None:
        from arcagent.core.capability_registry import (
            CapabilityRegistry,
            ToolEntry,
        )

        reg = CapabilityRegistry()
        entry = ToolEntry(
            meta=_tool_meta("hello"),
            execute=_noop,
            source_path=Path("/tmp/h.py"),
            scan_root="builtins",
        )
        result = await reg.register_tool(entry)
        assert result.outcome == "added"

        got = await reg.get_tool("hello")
        assert got is entry

    async def test_register_replace_emits_replaced(self) -> None:
        from arcagent.core.capability_registry import (
            CapabilityRegistry,
            ToolEntry,
        )

        reg = CapabilityRegistry()
        first = ToolEntry(
            meta=_tool_meta("foo", version="1.0.0"),
            execute=_noop,
            source_path=Path("/a.py"),
            scan_root="builtins",
        )
        second = ToolEntry(
            meta=_tool_meta("foo", version="1.1.0"),
            execute=_noop,
            source_path=Path("/b.py"),
            scan_root="agent",
        )
        await reg.register_tool(first)
        result = await reg.register_tool(second)
        assert result.outcome == "replaced"
        assert result.previous_version == "1.0.0"
        assert (await reg.get_tool("foo")) is second


@pytest.mark.asyncio
class TestRegisterHook:
    async def test_hooks_fan_out_priority_ordered(self) -> None:
        from arcagent.core.capability_registry import (
            CapabilityRegistry,
            HookEntry,
        )

        reg = CapabilityRegistry()
        order: list[str] = []

        async def first_handler(ctx: object) -> None:
            order.append("first")

        async def default_handler(ctx: object) -> None:
            order.append("default")

        async def last_handler(ctx: object) -> None:
            order.append("last")

        await reg.register_hook(
            HookEntry(
                meta=HookMetadata(name="last", event="agent:ready", priority=110, trylast=True),
                handler=last_handler,
                source_path=Path("/last.py"),
                scan_root="builtins",
            )
        )
        await reg.register_hook(
            HookEntry(
                meta=HookMetadata(name="first", event="agent:ready", priority=90, tryfirst=True),
                handler=first_handler,
                source_path=Path("/first.py"),
                scan_root="builtins",
            )
        )
        await reg.register_hook(
            HookEntry(
                meta=HookMetadata(name="default", event="agent:ready", priority=100),
                handler=default_handler,
                source_path=Path("/d.py"),
                scan_root="builtins",
            )
        )

        hooks = await reg.get_hooks("agent:ready")
        assert [h.meta.name for h in hooks] == ["first", "default", "last"]

        # Fan-out: registry returns ordered list; caller is responsible
        # for invoking each. Verify by invoking in returned order.
        for h in hooks:
            await h.handler(None)
        assert order == ["first", "default", "last"]

    async def test_register_two_hooks_same_event_both_kept(self) -> None:
        """Hooks fan out — last-wins does NOT apply (R-004)."""
        from arcagent.core.capability_registry import (
            CapabilityRegistry,
            HookEntry,
        )

        reg = CapabilityRegistry()

        async def h1(ctx: object) -> None:
            return None

        async def h2(ctx: object) -> None:
            return None

        await reg.register_hook(
            HookEntry(
                meta=HookMetadata(name="h1", event="ev"),
                handler=h1,
                source_path=Path("/1.py"),
                scan_root="builtins",
            )
        )
        await reg.register_hook(
            HookEntry(
                meta=HookMetadata(name="h2", event="ev"),
                handler=h2,
                source_path=Path("/2.py"),
                scan_root="builtins",
            )
        )

        hooks = await reg.get_hooks("ev")
        assert len(hooks) == 2


@pytest.mark.asyncio
class TestRegisterBackgroundTask:
    async def test_replace_drains_old_task(self) -> None:
        """R-062 — register a new task with same name cancels the old."""
        from arcagent.core.capability_registry import (
            BackgroundTaskEntry,
            CapabilityRegistry,
        )

        reg = CapabilityRegistry()

        cancelled = asyncio.Event()

        async def loop_a(ctx: object) -> None:
            try:
                while True:
                    await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        async def loop_b(ctx: object) -> None:
            await asyncio.sleep(0.5)

        a = BackgroundTaskEntry(
            meta=BackgroundTaskMetadata(name="poll", interval=0.1),
            fn=loop_a,
            source_path=Path("/a.py"),
            scan_root="builtins",
        )
        b = BackgroundTaskEntry(
            meta=BackgroundTaskMetadata(name="poll", interval=0.1),
            fn=loop_b,
            source_path=Path("/b.py"),
            scan_root="agent",
        )

        await reg.register_task(a)
        assert a.task is not None and not a.task.done()

        await reg.register_task(b)
        assert cancelled.is_set()
        assert a.task is not None and a.task.done()

        # Cleanup
        await reg.unregister("background_task", "poll")


@pytest.mark.asyncio
class TestRegisterCapabilityClass:
    async def test_capability_class_setup_not_run_at_register(self) -> None:
        """Registry only stores; loader (1.6) calls setup. R-060."""
        from arcagent.core.capability_registry import (
            CapabilityRegistry,
            LifecycleEntry,
        )

        reg = CapabilityRegistry()

        class FakeCap:
            setup_called = False

            async def setup(self, ctx: object) -> None:
                FakeCap.setup_called = True

            async def teardown(self) -> None:
                return None

        instance = FakeCap()
        entry = LifecycleEntry(
            meta=CapabilityClassMetadata(name="fake"),
            instance=instance,
            source_path=Path("/f.py"),
            scan_root="agent",
        )
        await reg.register_capability(entry)
        assert FakeCap.setup_called is False
        assert (await reg.get_capability("fake")) is entry


@pytest.mark.asyncio
class TestSkillRegistration:
    async def test_skill_round_trip(self) -> None:
        from arcagent.core.capability_registry import (
            CapabilityRegistry,
            SkillEntry,
        )

        reg = CapabilityRegistry()
        skill = SkillEntry(
            name="create-tool",
            version="1.0.0",
            description="Author a new tool.",
            triggers=("add a tool", "extend yourself"),
            tools=("write", "reload"),
            location=Path("/skills/create-tool/SKILL.md"),
            scan_root="builtins",
        )
        result = await reg.register_skill(skill)
        assert result.outcome == "added"
        assert (await reg.get_skill("create-tool")) is skill


@pytest.mark.asyncio
class TestPromptManifestCache:
    async def test_manifest_xml_shape(self) -> None:
        from arcagent.core.capability_registry import (
            CapabilityRegistry,
            SkillEntry,
            ToolEntry,
        )

        reg = CapabilityRegistry()
        await reg.register_tool(
            ToolEntry(
                meta=_tool_meta("grep", version="1.0.0"),
                execute=_noop,
                source_path=Path("/grep.py"),
                scan_root="builtins",
            )
        )
        await reg.register_skill(
            SkillEntry(
                name="create-tool",
                version="1.0.0",
                description="Author a new tool",
                triggers=("add a tool",),
                tools=("write", "reload"),
                location=Path("/skills/create-tool/SKILL.md"),
                scan_root="builtins",
            )
        )

        xml = await reg.format_for_prompt()
        # nosec B314 — registry-generated XML is trusted output; no untrusted parsing.
        root = ET.fromstring(f"<root>{xml}</root>")  # noqa: S314
        tools = root.find("available-tools")
        skills = root.find("available-skills")
        assert tools is not None and skills is not None

        tool_el = tools.find("tool")
        assert tool_el is not None
        assert tool_el.attrib["name"] == "grep"
        assert tool_el.attrib["version"] == "1.0.0"
        assert tool_el.attrib["classification"] == "read_only"

        skill_el = skills.find("skill")
        assert skill_el is not None
        assert skill_el.attrib["name"] == "create-tool"
        assert skill_el.attrib["version"] == "1.0.0"

    async def test_manifest_cached_until_mutation(self) -> None:
        from arcagent.core.capability_registry import (
            CapabilityRegistry,
            ToolEntry,
        )

        reg = CapabilityRegistry()
        await reg.register_tool(
            ToolEntry(
                meta=_tool_meta("a"),
                execute=_noop,
                source_path=Path("/a.py"),
                scan_root="builtins",
            )
        )
        first = await reg.format_for_prompt()
        second = await reg.format_for_prompt()
        assert first is second  # identity = same cached string

        await reg.register_tool(
            ToolEntry(
                meta=_tool_meta("b"),
                execute=_noop,
                source_path=Path("/b.py"),
                scan_root="builtins",
            )
        )
        third = await reg.format_for_prompt()
        assert third is not first


@pytest.mark.asyncio
class TestArcrunTools:
    async def test_to_arcrun_tools_returns_arcrun_tool(self) -> None:
        from arcrun.types import Tool as ArcRunTool

        from arcagent.core.capability_registry import (
            CapabilityRegistry,
            ToolEntry,
        )

        reg = CapabilityRegistry()
        await reg.register_tool(
            ToolEntry(
                meta=_tool_meta("greet", classification="read_only"),
                execute=_noop,
                source_path=Path("/g.py"),
                scan_root="builtins",
            )
        )
        tools = await reg.to_arcrun_tools()
        assert len(tools) == 1
        assert isinstance(tools[0], ArcRunTool)
        assert tools[0].name == "greet"
        assert tools[0].parallel_safe is True  # read_only → parallel-safe

    async def test_state_modifying_is_not_parallel_safe(self) -> None:
        from arcagent.core.capability_registry import (
            CapabilityRegistry,
            ToolEntry,
        )

        reg = CapabilityRegistry()
        await reg.register_tool(
            ToolEntry(
                meta=_tool_meta("write", classification="state_modifying"),
                execute=_noop,
                source_path=Path("/w.py"),
                scan_root="builtins",
            )
        )
        tools = await reg.to_arcrun_tools()
        assert tools[0].parallel_safe is False


@pytest.mark.asyncio
class TestUnregister:
    async def test_unregister_removes_tool(self) -> None:
        from arcagent.core.capability_registry import (
            CapabilityRegistry,
            ToolEntry,
        )

        reg = CapabilityRegistry()
        await reg.register_tool(
            ToolEntry(
                meta=_tool_meta("x"),
                execute=_noop,
                source_path=Path("/x.py"),
                scan_root="builtins",
            )
        )
        await reg.unregister("tool", "x")
        assert (await reg.get_tool("x")) is None

    async def test_unregister_unknown_no_op(self) -> None:
        from arcagent.core.capability_registry import CapabilityRegistry

        reg = CapabilityRegistry()
        await reg.unregister("tool", "nope")  # must not raise


@pytest.mark.asyncio
class TestLockSemantics:
    async def test_concurrent_reads(self) -> None:
        """Multiple readers proceed concurrently."""
        from arcagent.core.capability_registry import (
            CapabilityRegistry,
            ToolEntry,
        )

        reg = CapabilityRegistry()
        await reg.register_tool(
            ToolEntry(
                meta=_tool_meta("a"),
                execute=_noop,
                source_path=Path("/a.py"),
                scan_root="builtins",
            )
        )

        async def reader() -> str | None:
            t = await reg.get_tool("a")
            return t.meta.name if t else None

        results = await asyncio.gather(*(reader() for _ in range(10)))
        assert all(r == "a" for r in results)

    async def test_writer_excludes_readers(self) -> None:
        """Writer holds an exclusive lock — concurrent reader sees the
        post-write state, never half-mutated."""
        from arcagent.core.capability_registry import (
            CapabilityRegistry,
            ToolEntry,
        )

        reg = CapabilityRegistry()

        async def writer() -> None:
            for i in range(5):
                await reg.register_tool(
                    ToolEntry(
                        meta=_tool_meta(f"t{i}"),
                        execute=_noop,
                        source_path=Path(f"/t{i}.py"),
                        scan_root="builtins",
                    )
                )

        async def reader() -> int:
            count = 0
            for _ in range(20):
                tool = await reg.get_tool("t0")
                if tool is not None:
                    count += 1
            return count

        await asyncio.gather(writer(), reader())
        # Final state has all 5 tools
        for i in range(5):
            assert (await reg.get_tool(f"t{i}")) is not None


@pytest.mark.asyncio
class TestEventEmission:
    async def test_capability_added_event_fires(self) -> None:
        from arcagent.core.capability_registry import (
            CapabilityRegistry,
            ToolEntry,
        )
        from arcagent.core.module_bus import ModuleBus

        bus = ModuleBus()
        events: list[str] = []

        async def sub(ctx: object) -> None:
            events.append(getattr(ctx, "event", ""))

        bus.subscribe(event="capability:added", handler=sub)

        reg = CapabilityRegistry(bus=bus)
        await reg.register_tool(
            ToolEntry(
                meta=_tool_meta("e"),
                execute=_noop,
                source_path=Path("/e.py"),
                scan_root="builtins",
            )
        )
        # Bus event fires asynchronously
        await asyncio.sleep(0)
        assert "capability:added" in events

    async def test_capability_replaced_event_fires(self) -> None:
        from arcagent.core.capability_registry import (
            CapabilityRegistry,
            ToolEntry,
        )
        from arcagent.core.module_bus import ModuleBus

        bus = ModuleBus()
        events: list[str] = []

        async def sub(ctx: object) -> None:
            events.append(getattr(ctx, "event", ""))

        bus.subscribe(event="capability:replaced", handler=sub)

        reg = CapabilityRegistry(bus=bus)
        await reg.register_tool(
            ToolEntry(
                meta=_tool_meta("e"),
                execute=_noop,
                source_path=Path("/e.py"),
                scan_root="builtins",
            )
        )
        await reg.register_tool(
            ToolEntry(
                meta=_tool_meta("e", version="2.0.0"),
                execute=_noop,
                source_path=Path("/e2.py"),
                scan_root="agent",
            )
        )
        await asyncio.sleep(0)
        assert "capability:replaced" in events

    async def test_capability_removed_event_fires(self) -> None:
        from arcagent.core.capability_registry import (
            CapabilityRegistry,
            ToolEntry,
        )
        from arcagent.core.module_bus import ModuleBus

        bus = ModuleBus()
        events: list[str] = []

        async def sub(ctx: object) -> None:
            events.append(getattr(ctx, "event", ""))

        bus.subscribe(event="capability:removed", handler=sub)

        reg = CapabilityRegistry(bus=bus)
        await reg.register_tool(
            ToolEntry(
                meta=_tool_meta("e"),
                execute=_noop,
                source_path=Path("/e.py"),
                scan_root="builtins",
            )
        )
        await reg.unregister("tool", "e")
        await asyncio.sleep(0)
        assert "capability:removed" in events
