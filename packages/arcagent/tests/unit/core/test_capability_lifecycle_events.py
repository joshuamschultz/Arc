"""SPEC-021 Task 1.10 — five capability lifecycle bus events.

The events:

  * ``capability:added``               (registry → register success)
  * ``capability:removed``             (registry → unregister)
  * ``capability:replaced``            (registry → overwrite)
  * ``capability:registration_failed`` (loader → AST / frontmatter reject)
  * ``capability:setup_failed``        (loader → @capability setup raised)

For each lifecycle path the corresponding event must fire exactly once
with the expected payload. Audit emission via ``arctrust.audit.emit``
goes through an optional sink the agent supplies; this test exercises
the wiring with an in-memory sink so we can assert event shape without
filesystem I/O.

The first three events were verified in test_capability_registry.py;
this file focuses on the loader-side ``registration_failed`` and
``setup_failed`` plus the audit-sink integration.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arcagent.core.capability_registry import (
    CapabilityRegistry,
    LifecycleEntry,
    ToolEntry,
)
from arcagent.core.module_bus import ModuleBus
from arcagent.tools._decorator import (
    CapabilityClassMetadata,
    ToolMetadata,
)


def _tool_meta(name: str, version: str = "1.0.0") -> ToolMetadata:
    return ToolMetadata(
        name=name,
        description="d",
        input_schema={"type": "object", "properties": {}},
        classification="read_only",
        version=version,
    )


async def _noop(**_: object) -> str:
    return "ok"


@pytest.mark.asyncio
class TestRegistrationFailedEvent:
    async def test_ast_failure_emits_registration_failed(self, tmp_path: Path) -> None:
        from arcagent.core.capability_loader import CapabilityLoader

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "bad.py").write_text("import os\n")

        bus = ModuleBus()
        events: list[tuple[str, dict]] = []

        async def sub(ctx: object) -> None:
            events.append(
                (
                    getattr(ctx, "event", ""),
                    dict(getattr(ctx, "data", {})),
                )
            )

        bus.subscribe(event="capability:registration_failed", handler=sub)

        reg = CapabilityRegistry(bus=bus)
        loader = CapabilityLoader(scan_roots=[("workspace", workspace)], registry=reg, bus=bus)
        await loader.reload()

        names = [e for e, _ in events]
        assert names.count("capability:registration_failed") == 1
        _, payload = events[0]
        assert payload["path"].endswith("bad.py")
        assert "category" in payload or "reason" in payload

    async def test_skill_missing_frontmatter_emits_failed(self, tmp_path: Path) -> None:
        from arcagent.core.capability_loader import CapabilityLoader

        builtins_root = tmp_path / "builtins"
        skill_folder = builtins_root / "broken-skill"
        skill_folder.mkdir(parents=True)
        # SKILL.md without frontmatter at all
        (skill_folder / "SKILL.md").write_text("just plain text\n")

        bus = ModuleBus()
        events: list[str] = []

        async def sub(ctx: object) -> None:
            events.append(getattr(ctx, "event", ""))

        bus.subscribe(event="capability:registration_failed", handler=sub)

        reg = CapabilityRegistry(bus=bus)
        loader = CapabilityLoader(
            scan_roots=[("builtins", builtins_root)],
            registry=reg,
            bus=bus,
        )
        await loader.reload()
        assert events.count("capability:registration_failed") == 1


@pytest.mark.asyncio
class TestSetupFailedEvent:
    async def test_setup_raise_emits_setup_failed(self, tmp_path: Path) -> None:
        from arcagent.core.capability_loader import CapabilityLoader

        bus = ModuleBus()
        events: list[tuple[str, dict]] = []

        async def sub(ctx: object) -> None:
            events.append(
                (
                    getattr(ctx, "event", ""),
                    dict(getattr(ctx, "data", {})),
                )
            )

        bus.subscribe(event="capability:setup_failed", handler=sub)

        reg = CapabilityRegistry(bus=bus)

        class Boom:
            async def setup(self, ctx: object) -> None:
                raise RuntimeError("kaboom")

            async def teardown(self) -> None:
                return None

        await reg.register_capability(
            LifecycleEntry(
                meta=CapabilityClassMetadata(name="boomer"),
                instance=Boom(),
                source_path=Path("/x.py"),
                scan_root="builtins",
            )
        )
        loader = CapabilityLoader(scan_roots=[("builtins", tmp_path)], registry=reg, bus=bus)

        with pytest.raises(RuntimeError, match="kaboom"):
            await loader.start_lifecycles()

        assert any(e == "capability:setup_failed" for e, _ in events)
        _, payload = next((e, p) for e, p in events if e == "capability:setup_failed")
        assert payload["name"] == "boomer"
        assert "RuntimeError" in payload.get("exception_type", "")


@pytest.mark.asyncio
class TestAuditSinkIntegration:
    async def test_register_emits_audit_event(self) -> None:
        from typing import ClassVar

        from arctrust.audit import AuditEvent, AuditSink

        class Captured:
            events: ClassVar[list[AuditEvent]] = []

            def emit(self, event: AuditEvent) -> None:
                Captured.events.append(event)

        sink: AuditSink = Captured()
        Captured.events = []

        reg = CapabilityRegistry(audit_sink=sink)
        await reg.register_tool(
            ToolEntry(
                meta=_tool_meta("a"),
                execute=_noop,
                source_path=Path("/a.py"),
                scan_root="builtins",
            )
        )
        assert any(e.action == "capability.added" for e in Captured.events)
        assert any(e.target == "a" for e in Captured.events)

    async def test_replace_emits_audit_event(self) -> None:
        from typing import ClassVar

        from arctrust.audit import AuditEvent, AuditSink

        class Captured:
            events: ClassVar[list[AuditEvent]] = []

            def emit(self, event: AuditEvent) -> None:
                Captured.events.append(event)

        sink: AuditSink = Captured()
        Captured.events = []

        reg = CapabilityRegistry(audit_sink=sink)
        await reg.register_tool(
            ToolEntry(
                meta=_tool_meta("a"),
                execute=_noop,
                source_path=Path("/a.py"),
                scan_root="builtins",
            )
        )
        await reg.register_tool(
            ToolEntry(
                meta=_tool_meta("a", version="2.0.0"),
                execute=_noop,
                source_path=Path("/b.py"),
                scan_root="agent",
            )
        )
        actions = {e.action for e in Captured.events}
        assert "capability.replaced" in actions
