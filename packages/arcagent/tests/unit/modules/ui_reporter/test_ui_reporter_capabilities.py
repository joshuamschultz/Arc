"""SPEC-021 Task 3.8 — ui_reporter module decorator-form tests.

The new ``capabilities.py`` exposes 22 ``@hook``-decorated functions:

  * 3 LLM events
  * 14 agent / orchestration events
  * 2 scheduler events
  * 5 capability lifecycle events (R-050, new in SPEC-021)

This file verifies:

  1. All hooks register via :class:`CapabilityLoader` against the
     ui_reporter module directory.
  2. Each hook routes its event payload through the runtime's single
     ``emit_to_arcui`` helper, which in turn calls the WebSocket
     transport's ``send_event``.
  3. The ``capability:*`` events reach arcui — the new R-050 surface.
  4. Layer classification matches the legacy ``UIReporterModule``.
  5. Hooks raise when called before ``_runtime.configure(...)``.

Legacy :class:`UIReporterModule` tests in ``test_ui_reporter.py`` and
``test_auto_enable.py`` continue to verify behaviour at the wrapper
level.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from arcagent.core.capability_loader import CapabilityLoader
from arcagent.core.capability_registry import CapabilityRegistry
from arcagent.modules.ui_reporter import _runtime

_ALL_SUBSCRIBED_EVENTS = (
    # LLM
    "llm:call_complete",
    "llm:config_change",
    "llm:circuit_change",
    # Agent / orchestration
    "agent:init",
    "agent:ready",
    "agent:shutdown",
    "agent:pre_respond",
    "agent:post_respond",
    "agent:error",
    "agent:extensions_loaded",
    "agent:skills_loaded",
    "agent:tools_reloaded",
    "agent:pre_tool",
    "agent:post_tool",
    "agent:pre_plan",
    "agent:post_plan",
    "agent:pre_compaction",
    # Scheduler
    "schedule:completed",
    "schedule:failed",
    # Capability lifecycle (SPEC-021 R-050)
    "capability:added",
    "capability:removed",
    "capability:replaced",
    "capability:registration_failed",
    "capability:setup_failed",
)


@pytest.fixture(autouse=True)
def _reset_runtime() -> Iterator[None]:
    _runtime.reset()
    yield
    _runtime.reset()


@pytest.fixture
def configured(tmp_path: Path) -> AsyncMock:
    """Configure ui_reporter with a mocked WebSocket transport.

    Returns the mock transport so tests can assert send_event calls.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mock_transport = AsyncMock()
    mock_transport.send_event = AsyncMock()
    _runtime.configure(
        config={"enabled": True, "token": "test-token"},
        workspace=workspace,
        transport=mock_transport,
        agent_name="test-agent",
        agent_id="did:arc:test",
        source_id="did:arc:test",
    )
    return mock_transport


@pytest.mark.asyncio
class TestLoaderRegistration:
    async def test_all_hooks_register(self) -> None:
        """All 24 events must have at least one registered hook."""
        from arcagent.modules.ui_reporter import capabilities as caps

        module_dir = Path(caps.__file__).parent
        reg = CapabilityRegistry()
        loader = CapabilityLoader(scan_roots=[("ui_reporter", module_dir)], registry=reg)
        await loader.scan_and_register()

        for event in _ALL_SUBSCRIBED_EVENTS:
            hooks = await reg.get_hooks(event)
            assert hooks, f"no hook registered for {event!r}"

    async def test_all_hooks_observational_priority(self) -> None:
        """All ui_reporter hooks run at priority 200 (observational)."""
        from arcagent.modules.ui_reporter import capabilities as caps

        module_dir = Path(caps.__file__).parent
        reg = CapabilityRegistry()
        loader = CapabilityLoader(scan_roots=[("ui_reporter", module_dir)], registry=reg)
        await loader.scan_and_register()

        for event in _ALL_SUBSCRIBED_EVENTS:
            hooks = await reg.get_hooks(event)
            for h in hooks:
                # Only test ui_reporter-owned hooks; the registry can
                # legitimately hold others if a future module also
                # subscribes to the same event.
                if "ui_reporter" not in str(h.source_path):
                    continue
                assert h.meta.priority == 200, (
                    f"{h.meta.name} ({event}) has priority {h.meta.priority}, expected 200"
                )


# --- emit_to_arcui plumbing -----------------------------------------------


@pytest.mark.asyncio
class TestEmitToArcUI:
    async def test_capability_added_reaches_transport(self, configured: AsyncMock) -> None:
        """The new R-050 capability:added event must reach arcui."""
        from arcagent.modules.ui_reporter.capabilities import on_capability_added

        ctx = SimpleNamespace(
            event="capability:added",
            data={
                "name": "read_file",
                "kind": "tool",
                "scan_root": "builtins",
                "source_path": "/path/to/read.py",
            },
        )
        await on_capability_added(ctx)

        configured.send_event.assert_called_once()
        agent_id, ui_event = configured.send_event.call_args[0]
        assert agent_id == "did:arc:test"
        # capability:* routes through the "agent" layer for UIEvent
        # schema compatibility (Literal field on UIEvent.layer).
        assert ui_event.layer == "agent"
        assert ui_event.event_type == "added"
        assert ui_event.data["name"] == "read_file"

    async def test_capability_removed_reaches_transport(self, configured: AsyncMock) -> None:
        from arcagent.modules.ui_reporter.capabilities import on_capability_removed

        ctx = SimpleNamespace(
            event="capability:removed",
            data={"kind": "tool", "name": "read_file", "version": "1.0.0"},
        )
        await on_capability_removed(ctx)

        configured.send_event.assert_called_once()
        ui_event = configured.send_event.call_args[0][1]
        assert ui_event.event_type == "removed"

    async def test_llm_event_classified_as_llm_layer(self, configured: AsyncMock) -> None:
        from arcagent.modules.ui_reporter.capabilities import on_llm_call_complete

        ctx = SimpleNamespace(
            event="llm:call_complete",
            data={"model": "gpt-4", "tokens": 100},
        )
        await on_llm_call_complete(ctx)

        ui_event = configured.send_event.call_args[0][1]
        assert ui_event.layer == "llm"
        assert ui_event.event_type == "call_complete"

    async def test_agent_pre_tool_classified_as_run_layer(self, configured: AsyncMock) -> None:
        """Tool/plan bridge events from arcrun map to the run layer."""
        from arcagent.modules.ui_reporter.capabilities import on_agent_pre_tool

        ctx = SimpleNamespace(
            event="agent:pre_tool",
            data={"tool": "search"},
        )
        await on_agent_pre_tool(ctx)

        ui_event = configured.send_event.call_args[0][1]
        assert ui_event.layer == "run"
        assert ui_event.event_type == "pre_tool"

    async def test_agent_ready_classified_as_agent_layer(self, configured: AsyncMock) -> None:
        from arcagent.modules.ui_reporter.capabilities import on_agent_ready

        ctx = SimpleNamespace(event="agent:ready", data={})
        await on_agent_ready(ctx)

        ui_event = configured.send_event.call_args[0][1]
        assert ui_event.layer == "agent"

    async def test_schedule_event_classified_as_scheduler_layer(
        self, configured: AsyncMock
    ) -> None:
        from arcagent.modules.ui_reporter.capabilities import on_schedule_completed

        ctx = SimpleNamespace(
            event="schedule:completed",
            data={"schedule_id": "s-1", "result": "ok"},
        )
        await on_schedule_completed(ctx)

        ui_event = configured.send_event.call_args[0][1]
        assert ui_event.layer == "scheduler"
        assert ui_event.event_type == "completed"

    async def test_sequence_increments_across_hooks(self, configured: AsyncMock) -> None:
        """The runtime sequence must monotonically increase across emits."""
        from arcagent.modules.ui_reporter.capabilities import (
            on_agent_ready,
            on_llm_call_complete,
        )

        await on_agent_ready(SimpleNamespace(event="agent:ready", data={}))
        await on_llm_call_complete(SimpleNamespace(event="llm:call_complete", data={}))

        first_seq = configured.send_event.call_args_list[0][0][1].sequence
        second_seq = configured.send_event.call_args_list[1][0][1].sequence
        assert second_seq == first_seq + 1

    async def test_no_transport_is_silent_noop(self, tmp_path: Path) -> None:
        """When transport is None (no UI running), emit must not raise."""
        _runtime.configure(
            config={"enabled": True, "token": ""},
            workspace=tmp_path,
            transport=None,
            agent_name="test",
        )
        from arcagent.modules.ui_reporter.capabilities import on_capability_added

        # Must not raise — agent runs even when arcui is offline.
        await on_capability_added(SimpleNamespace(event="capability:added", data={"name": "x"}))


# --- Runtime contract -----------------------------------------------------


@pytest.mark.asyncio
class TestRuntimeContract:
    async def test_unconfigured_raises(self) -> None:
        """Hooks must raise a clear error before configure() is called."""
        from arcagent.modules.ui_reporter.capabilities import on_capability_added

        with pytest.raises(RuntimeError, match="before runtime is configured"):
            await on_capability_added(SimpleNamespace(event="capability:added", data={}))


# --- Layer classification (drop-in equivalence with legacy module) --------


class TestLayerClassification:
    @pytest.mark.parametrize(
        "event,expected_layer",
        [
            ("llm:call_complete", "llm"),
            ("llm:config_change", "llm"),
            ("llm:circuit_change", "llm"),
            ("agent:pre_tool", "run"),
            ("agent:post_tool", "run"),
            ("agent:pre_plan", "run"),
            ("agent:post_plan", "run"),
            ("agent:init", "agent"),
            ("agent:ready", "agent"),
            ("agent:shutdown", "agent"),
            ("agent:pre_respond", "agent"),
            ("agent:post_respond", "agent"),
            ("agent:error", "agent"),
            ("agent:extensions_loaded", "agent"),
            ("agent:skills_loaded", "agent"),
            ("schedule:completed", "scheduler"),
            ("schedule:failed", "scheduler"),
            ("capability:added", "agent"),
            ("capability:removed", "agent"),
            ("capability:replaced", "agent"),
            ("capability:registration_failed", "agent"),
            ("capability:setup_failed", "agent"),
        ],
    )
    def test_event_layer_mapping(self, event: str, expected_layer: str) -> None:
        assert _runtime.classify_layer(event) == expected_layer
