"""SPEC-021 Task 1.2 — ``@hook`` / ``@background_task`` / ``@capability``.

Three new decorators alongside ``@tool``. Each stamps a frozen
:class:`CapabilityMetadata` variant on the decorated callable or class
under ``_arc_capability_meta``. The loader (Task 1.6) reads these
stamps without touching runtime state.

A ``@capability`` class may contain ``@tool``-decorated methods. After
the class is decorated, both stamps must coexist:

  * the class itself carries a ``CapabilityClassMetadata`` stamp
  * each decorated method still carries its own ``ToolMetadata`` stamp

The loader binds the method tools to a class instance at registration
time (1.6 territory).
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest


class TestHookDecorator:
    def test_hook_stamps_metadata(self) -> None:
        from arcagent.tools._decorator import hook

        @hook(event="agent:ready")
        async def on_ready(ctx: object) -> None:
            return None

        meta = on_ready._arc_capability_meta  # type: ignore[attr-defined]
        assert meta.kind == "hook"
        assert meta.event == "agent:ready"
        assert meta.name == "on_ready"

    def test_hook_priority_defaults_100(self) -> None:
        from arcagent.tools._decorator import hook

        @hook(event="agent:shutdown")
        async def on_shutdown(ctx: object) -> None:
            return None

        meta = on_shutdown._arc_capability_meta  # type: ignore[attr-defined]
        assert meta.priority == 100
        assert meta.tryfirst is False
        assert meta.trylast is False

    def test_hook_priority_override(self) -> None:
        from arcagent.tools._decorator import hook

        @hook(event="agent:ready", priority=50)
        async def early(ctx: object) -> None:
            return None

        assert early._arc_capability_meta.priority == 50  # type: ignore[attr-defined]

    def test_hook_tryfirst_lowers_priority(self) -> None:
        """``tryfirst=True`` is a pluggy-style ordering override."""
        from arcagent.tools._decorator import hook

        @hook(event="agent:ready", tryfirst=True)
        async def first(ctx: object) -> None:
            return None

        meta = first._arc_capability_meta  # type: ignore[attr-defined]
        assert meta.tryfirst is True
        assert meta.priority == 90

    def test_hook_trylast_raises_priority(self) -> None:
        from arcagent.tools._decorator import hook

        @hook(event="agent:ready", trylast=True)
        async def last(ctx: object) -> None:
            return None

        meta = last._arc_capability_meta  # type: ignore[attr-defined]
        assert meta.trylast is True
        assert meta.priority == 110

    def test_hook_explicit_name_override(self) -> None:
        from arcagent.tools._decorator import hook

        @hook(event="agent:ready", name="custom-name")
        async def fn(ctx: object) -> None:
            return None

        assert fn._arc_capability_meta.name == "custom-name"  # type: ignore[attr-defined]

    def test_hook_metadata_is_frozen(self) -> None:
        from arcagent.tools._decorator import hook

        @hook(event="agent:ready")
        async def fn(ctx: object) -> None:
            return None

        with pytest.raises(FrozenInstanceError):
            fn._arc_capability_meta.priority = 1  # type: ignore[attr-defined,misc]

    def test_hook_preserves_callable(self) -> None:
        import asyncio

        from arcagent.tools._decorator import hook

        @hook(event="agent:ready")
        async def returns_42(ctx: object) -> int:
            return 42

        assert asyncio.run(returns_42(None)) == 42

    def test_hook_tryfirst_and_trylast_mutually_exclusive(self) -> None:
        from arcagent.tools._decorator import hook

        with pytest.raises(ValueError, match="tryfirst.*trylast"):

            @hook(event="agent:ready", tryfirst=True, trylast=True)
            async def fn(ctx: object) -> None:
                return None


class TestBackgroundTaskDecorator:
    def test_background_task_stamps_metadata(self) -> None:
        from arcagent.tools._decorator import background_task

        @background_task(name="poll", interval=60.0)
        async def poll(ctx: object) -> None:
            return None

        meta = poll._arc_capability_meta  # type: ignore[attr-defined]
        assert meta.kind == "background_task"
        assert meta.name == "poll"
        assert meta.interval == 60.0

    def test_background_task_default_name(self) -> None:
        from arcagent.tools._decorator import background_task

        @background_task(interval=5.0)
        async def heartbeat(ctx: object) -> None:
            return None

        assert heartbeat._arc_capability_meta.name == "heartbeat"  # type: ignore[attr-defined]

    def test_background_task_rejects_non_positive_interval(self) -> None:
        from arcagent.tools._decorator import background_task

        with pytest.raises(ValueError, match="interval must be > 0"):

            @background_task(interval=0)
            async def fn(ctx: object) -> None:
                return None

        with pytest.raises(ValueError, match="interval must be > 0"):

            @background_task(interval=-1.0)
            async def fn2(ctx: object) -> None:
                return None

    def test_background_task_metadata_is_frozen(self) -> None:
        from arcagent.tools._decorator import background_task

        @background_task(interval=10.0)
        async def fn(ctx: object) -> None:
            return None

        with pytest.raises(FrozenInstanceError):
            fn._arc_capability_meta.interval = 1.0  # type: ignore[attr-defined,misc]


class TestCapabilityClassDecorator:
    def test_capability_class_stamps_metadata(self) -> None:
        from arcagent.tools._decorator import capability

        @capability(name="browser")
        class BrowserCapability:
            async def setup(self, ctx: object) -> None:
                return None

            async def teardown(self) -> None:
                return None

        meta = BrowserCapability._arc_capability_meta  # type: ignore[attr-defined]
        assert meta.kind == "capability"
        assert meta.name == "browser"
        assert meta.depends_on == ()

    def test_capability_class_default_name_from_class(self) -> None:
        from arcagent.tools._decorator import capability

        @capability()
        class Scheduler:
            pass

        assert Scheduler._arc_capability_meta.name == "Scheduler"  # type: ignore[attr-defined]

    def test_capability_class_depends_on(self) -> None:
        from arcagent.tools._decorator import capability

        @capability(name="slack", depends_on=["scheduler", "memory"])
        class Slack:
            pass

        meta = Slack._arc_capability_meta  # type: ignore[attr-defined]
        assert meta.depends_on == ("scheduler", "memory")

    def test_capability_class_with_tool_method(self) -> None:
        """Class carries capability stamp; method keeps its own tool stamp."""
        from arcagent.tools._decorator import capability, tool

        @capability(name="memory")
        class Memory:
            @tool(description="store an item", classification="state_modifying")
            async def store(self, key: str, value: str) -> None:
                return None

        # Class-level stamp
        cls_meta = Memory._arc_capability_meta  # type: ignore[attr-defined]
        assert cls_meta.kind == "capability"
        assert cls_meta.name == "memory"

        # Method-level stamp survives
        method_meta = Memory.store._arc_capability_meta  # type: ignore[attr-defined]
        assert method_meta.kind == "tool"
        assert method_meta.name == "store"
        # Schema excludes ``self``
        assert "self" not in method_meta.input_schema["properties"]
        assert set(method_meta.input_schema["required"]) == {"key", "value"}

    def test_capability_class_metadata_is_frozen(self) -> None:
        from arcagent.tools._decorator import capability

        @capability(name="foo")
        class Foo:
            pass

        with pytest.raises(FrozenInstanceError):
            Foo._arc_capability_meta.name = "bar"  # type: ignore[attr-defined,misc]


class TestCapabilityMetadataUnion:
    """All four kinds round-trip through the ``CapabilityMetadata`` alias."""

    def test_kind_discriminator_distinguishes_all_four(self) -> None:
        from arcagent.tools._decorator import (
            background_task,
            capability,
            hook,
            tool,
        )

        @tool(description="t")
        async def t() -> None:
            return None

        @hook(event="e")
        async def h(ctx: object) -> None:
            return None

        @background_task(interval=1.0)
        async def b(ctx: object) -> None:
            return None

        @capability(name="c")
        class C:
            pass

        kinds = {
            t._arc_capability_meta.kind,  # type: ignore[attr-defined]
            h._arc_capability_meta.kind,  # type: ignore[attr-defined]
            b._arc_capability_meta.kind,  # type: ignore[attr-defined]
            C._arc_capability_meta.kind,  # type: ignore[attr-defined]
        }
        assert kinds == {"tool", "hook", "background_task", "capability"}
