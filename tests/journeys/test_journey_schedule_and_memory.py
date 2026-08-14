"""Journeys: "remind me every morning" and "what did I tell you last week".

Both are things a user sets up once and relies on for weeks, so both have the
same failure mode: they appear to work in the moment and are quietly gone later.
A schedule that is accepted but never persisted, or a memory captured into a
store nothing reads back, produces no error at any point — the agent simply
stops doing the thing, and the operator finds out much later.

The assertions therefore land on durable state read back from disk through a
freshly constructed store, not on the tool's own success string.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from .conftest import Deployment, ScriptedLLM, ScriptedTurn
from .test_journey_modules import _SOURCE_CATALOG, install_modules, start_agent


@pytest.fixture(autouse=True)
def _module_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_MODULE_SOURCE", str(_SOURCE_CATALOG))


async def _with_modules(
    deployment: Deployment,
    enable_modules: Any,
    *modules: str,
    config: dict[str, dict[str, Any]] | None = None,
) -> Any:
    """Enable modules, run the real install, and start the agent."""
    enable_modules(*modules, config=config)
    rows = install_modules(deployment)
    assert not [row for row in rows if "REFUSED" in row], f"install refused: {rows}"
    return await start_agent(deployment)


async def _drive(agent: Any, text: str, *, key: str = "journey") -> str:
    session = await agent.session(key)
    out: list[str] = []
    async for event in agent.run(text, session=session):
        piece = getattr(event, "text", None)
        if isinstance(piece, str):
            out.append(piece)
    return "".join(out)


# ---------------------------------------------------------------------------
# "Remind me every morning"
# ---------------------------------------------------------------------------


async def test_a_schedule_the_agent_creates_survives_a_restart(
    deployment: Deployment, enable_modules: Any, scripted_llm: ScriptedLLM
) -> None:
    """The user asks for a recurring job; it must still exist after a restart.

    Read back through a brand-new ``ScheduleStore`` over the file on disk —
    asserting against the live agent's in-memory copy would pass for a schedule
    that was never written, which is exactly the state that loses a user's
    standing reminder on the next deploy.
    """
    from arcagent import ScheduleStore

    agent = await _with_modules(deployment, enable_modules, "scheduler")
    try:
        scripted_llm.replies.extend(
            [
                ScriptedTurn(
                    tool="schedule_create",
                    args={
                        "type": "cron",
                        "expression": "0 9 * * *",
                        "prompt": "Send the morning briefing",
                    },
                ),
                "Scheduled for 9am daily.",
            ]
        )
        await _drive(agent, "brief me every morning at 9")
        workspace = agent._workspace
    finally:
        await agent.shutdown()

    store_files = list(workspace.rglob("schedules.json"))
    assert store_files, f"no schedule store was written under {workspace}"
    entries = ScheduleStore(store_files[0]).load()
    assert entries, "the schedule was accepted but nothing was persisted"
    assert any("briefing" in (entry.prompt or "").lower() for entry in entries), (
        f"the persisted schedules do not hold the user's request: {entries}"
    )


async def test_a_corrupt_schedule_file_does_not_silently_erase_the_rest(
    deployment: Deployment, enable_modules: Any, scripted_llm: ScriptedLLM
) -> None:
    """One bad row must not take the user's other schedules with it.

    A single malformed entry once stopped every schedule an agent had, silently,
    until the next restart. Whatever the store does with the bad row, the valid
    one beside it must still load.
    """
    from arcagent import ScheduleStore

    agent = await _with_modules(deployment, enable_modules, "scheduler")
    try:
        scripted_llm.replies.extend(
            [
                ScriptedTurn(
                    tool="schedule_create",
                    args={
                        "type": "cron",
                        "expression": "0 9 * * *",
                        "prompt": "Send the morning briefing",
                    },
                ),
                "Scheduled.",
            ]
        )
        await _drive(agent, "brief me every morning at 9")
        workspace = agent._workspace
    finally:
        await agent.shutdown()

    path = next(iter(workspace.rglob("schedules.json")))
    good = json.loads(path.read_text(encoding="utf-8"))
    assert good, "nothing was persisted to corrupt"
    path.write_text(json.dumps([{"nonsense": True}, *good]), encoding="utf-8")

    survivors = ScheduleStore(path).load()
    assert survivors, "one malformed row erased every valid schedule beside it"


# ---------------------------------------------------------------------------
# "What did I tell you last week"
# ---------------------------------------------------------------------------


def _arcmemory_installed() -> bool:
    """Is the optional memory engine present in this environment?"""
    import importlib.util

    return importlib.util.find_spec("arcmemory") is not None


async def test_recall_is_available_or_says_plainly_that_it_is_not(
    deployment: Deployment, enable_modules: Any, scripted_llm: ScriptedLLM
) -> None:
    """Whatever recall does, it must never answer a silent "nothing found".

    ``arcmemory`` is an optional extra, so the memory module legitimately runs on
    a ``NullBrain`` when it is absent. The danger is not that recall is off — it
    is that "off" and "searched and found nothing" read identically to the model
    and to the user, which is how a fleet ran for weeks with memory quietly doing
    nothing. The module must therefore say which of the two it is.

    Both environments are covered: with the engine installed recall must really
    be active; without it, the answer must name the reason.
    """
    from arcagent.modules.memory.capabilities import memory_search

    agent = await _with_modules(
        deployment, enable_modules, "memory", config={"memory": {"brain": "arcmemory"}}
    )
    try:
        assert "memory_search" in agent._tool_registry.tools

        # The tool is reachable through a real turn, not just present in a dict.
        scripted_llm.replies.extend(
            [
                ScriptedTurn(tool="memory_search", args={"query": "quarterly numbers"}),
                "Here is what I found.",
            ]
        )
        assert "Here is what I found." in await _drive(agent, "what do you remember?")

        answer = str(await memory_search("anything")).lower()
        if _arcmemory_installed():
            assert "not enabled" not in answer, (
                "arcmemory is installed but the module never activated — recall is dead"
            )
        else:
            assert "not enabled" in answer, (
                "recall is disabled but reports it as an empty result, which is "
                f"indistinguishable from a real miss: {answer!r}"
            )
    finally:
        await agent.shutdown()


async def test_what_the_user_says_is_captured_for_later_recall(
    deployment: Deployment, enable_modules: Any, scripted_llm: ScriptedLLM
) -> None:
    """A turn must leave something behind for memory to consolidate from.

    Capture is the half a user never sees fail: recall can only ever return what
    an earlier turn stored, so a broken capture path looks exactly like an agent
    that was never told anything.
    """
    agent = await _with_modules(deployment, enable_modules, "memory")
    try:
        await _drive(agent, "The launch date moved to the 14th.")
        workspace = agent._workspace
    finally:
        await agent.shutdown()

    written = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in workspace.rglob("*")
        if path.is_file() and path.suffix in {".json", ".jsonl", ".md", ".txt"}
    )
    assert "launch date moved" in written, (
        f"nothing under {workspace} retained the user's statement for recall"
    )
