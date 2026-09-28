"""A read-only dynamic child keeps the agent's read-only tools, and only those.

arcrun narrows a ``dynamic`` child in ``read_only`` mode (the default) to the
parent's tools classified ``read_only``. The classification is the agent's own
(``@tool(classification=...)``); if it is dropped where the agent hands its tools
to arcrun, every tool reads as state-modifying and a read-only child gets none —
it cannot even read a file. Driven through a real started agent; only the model
wire is faked (the conformance harness).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.test_prompt_edit_conformance import (
    Harness,
    JevWire,
    ScriptedModel,
    Turn,
    _deploy,
    _flatten,
    _started,
    install_wires,
)

_CHILD_TASK = "Summarize the Acme renewal."


@pytest.fixture
def wires(monkeypatch: pytest.MonkeyPatch) -> tuple[ScriptedModel, JevWire]:
    return install_wires(monkeypatch)


async def test_read_only_dynamic_child_gets_read_tools_and_never_bash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wires: tuple[ScriptedModel, JevWire]
) -> None:
    script = f'result = agent("{_CHILD_TASK}")\ncomplete(result["output"])\n'
    harness = Harness(agent_dir=_deploy(tmp_path, monkeypatch, ""), llm=wires[0], jev=wires[1])
    harness.llm.turns.extend(
        [Turn(tool="emit_script", args={"source": script}), Turn(text="Acme renews at 42k.")]
    )

    async with _started(harness):
        await harness.say("Research the Acme renewal.", strategies=["dynamic"])

    child = [
        request
        for request in harness.llm.requests
        if any(
            getattr(message, "role", "") == "user" and _CHILD_TASK in "".join(_flatten(message))
            for message in request["messages"]
        )
    ]
    assert child, "the dynamic child never called the model"
    offered = {getattr(tool, "name", "") for request in child for tool in request["tools"] or []}
    assert {"read", "ls", "grep", "find"} <= offered, f"read-only child was offered {offered}"
    assert not offered & {"bash", "write", "edit"}, f"read-only child was offered {offered}"
