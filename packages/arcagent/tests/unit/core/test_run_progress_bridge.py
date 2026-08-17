"""The arcrun bridge carries dynamic-run events, with the turn's origin stamped on.

arcrun announces every stage of a ``dynamic`` run on its own bus and knows
nothing about channels — correct, and the reason the origin has to be attached on
this side. It is read once, in the dispatch task that bound it, and travels with
the event, so no consumer downstream is ever left working out where to answer.
"""

from __future__ import annotations

import asyncio
from typing import Any

import arcrun

from arcagent.core.model_manager import create_arcrun_bridge
from arcagent.core.module_bus import ModuleBus


class _Recorder:
    """Collects everything emitted on the module bus."""

    def __init__(self, bus: ModuleBus, event: str) -> None:
        self.seen: list[dict[str, Any]] = []
        bus.subscribe(event=event, handler=self._handle, module_name="test")

    async def _handle(self, ctx: Any) -> None:
        self.seen.append(ctx.data)


def _event(event_type: str, **data: Any) -> arcrun.Event:
    return arcrun.Event(type=event_type, timestamp=0.0, run_id="run-1", data=data)


async def _drain() -> None:
    """The bridge schedules emissions as detached tasks."""
    await asyncio.sleep(0)
    await asyncio.sleep(0)


class TestProgressForwarding:
    async def test_dynamic_events_arrive_with_the_turn_origin(self) -> None:
        bus = ModuleBus()
        recorder = _Recorder(bus, "agent:run_progress")
        bridge = create_arcrun_bridge(bus, reply_target="telegram:44")

        bridge(_event("dynamic.agent.start", run_id="a1", label="scout", depth=1))
        await _drain()

        assert recorder.seen == [
            {
                "event": "dynamic.agent.start",
                "reply_target": "telegram:44",
                "data": {"run_id": "a1", "label": "scout", "depth": 1},
            }
        ]

    async def test_an_origin_less_run_forwards_none_rather_than_a_guess(self) -> None:
        bus = ModuleBus()
        recorder = _Recorder(bus, "agent:run_progress")
        bridge = create_arcrun_bridge(bus)

        bridge(_event("dynamic.phase", title="Read the filings"))
        await _drain()

        assert recorder.seen[0]["reply_target"] is None

    async def test_ordinary_loop_events_keep_their_own_mapping(self) -> None:
        bus = ModuleBus()
        progress = _Recorder(bus, "agent:run_progress")
        tools = _Recorder(bus, "agent:pre_tool")
        bridge = create_arcrun_bridge(bus, reply_target="web:1")

        bridge(_event("tool.start", name="read"))
        await _drain()

        assert tools.seen == [{"name": "read"}]
        assert progress.seen == []

    async def test_unmapped_events_are_dropped(self) -> None:
        bus = ModuleBus()
        progress = _Recorder(bus, "agent:run_progress")
        bridge = create_arcrun_bridge(bus, reply_target="web:1")

        bridge(_event("llm.call", model="x"))
        await _drain()

        assert progress.seen == []
