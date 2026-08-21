"""RED — decision_point moment emission at the loop decision points (SPEC-072 COMP-004).

The loop announces a decision point so a Brain can recall right before the agent acts:

* ``turn.start`` → an ``agent:moment`` kind=decision_point, point=pre_plan (the default
  site — cues come from the working set the Brain already holds);
* ``tool.start`` → an ``agent:moment`` kind=decision_point, point=pre_tool carrying the
  tool name + args (the opt-in, finer site).

``decision_point_moment`` is the pure payload builder (deterministic); the arcrun bridge
schedules it onto the bus alongside its existing lifecycle mappings. Payload stays
primitive ({kind, cues, text, session_id, point}).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from arcagent.core.background_tasks import BackgroundTaskSupervisor
from arcagent.core.model_manager import create_arcrun_bridge, decision_point_moment
from arcagent.core.module_bus import ModuleBus


def _event(event_type: str, data: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(type=event_type, data=data)


def test_turn_start_builds_a_pre_plan_decision_point() -> None:
    moment = decision_point_moment(_event("turn.start", {"turn_number": 2}))
    assert moment is not None
    assert moment["kind"] == "decision_point"
    assert moment["point"] == "pre_plan"
    assert set(moment) >= {"kind", "cues", "text", "session_id"}


def test_tool_start_builds_a_pre_tool_decision_point_with_tool_cues() -> None:
    moment = decision_point_moment(_event("tool.start", {"tool": "web_search", "args": {"q": "x"}}))
    assert moment is not None
    assert moment["point"] == "pre_tool"
    assert moment["cues"] == ["web_search"]
    assert "web_search" in moment["text"]


def test_non_decision_events_build_no_moment() -> None:
    assert decision_point_moment(_event("turn.end", {})) is None
    assert decision_point_moment(_event("llm.call", {})) is None


class _Recorder:
    def __init__(self) -> None:
        self.moments: list[dict[str, Any]] = []

    async def __call__(self, ctx: Any) -> None:
        self.moments.append(dict(ctx.data))


async def test_bridge_emits_decision_point_moment_at_both_sites() -> None:
    bus = ModuleBus()
    recorder = _Recorder()
    bus.subscribe("agent:moment", recorder, module_name="rec")
    supervisor = BackgroundTaskSupervisor()
    bridge = create_arcrun_bridge(bus, task_supervisor=supervisor)

    bridge(_event("turn.start", {"turn_number": 1}))
    bridge(_event("tool.start", {"tool": "deploy", "args": {}}))
    # Let the fire-and-forget emit tasks run to completion (drain() would cancel them).
    while supervisor.task_count:
        await asyncio.sleep(0)

    sites = {(m.get("kind"), m.get("point")) for m in recorder.moments}
    assert ("decision_point", "pre_plan") in sites
    assert ("decision_point", "pre_tool") in sites
