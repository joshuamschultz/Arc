"""The agent must be TOLD to consult its procedures, not just be able to.

Adding `procedure_list`/`procedure_get` made the operator's playbooks reachable.
It did not make them reached: a model with a hundred tools does not call one it
was never pointed at, which is why an agent with 36 recorded procedures answered
from a skill and never opened the matching playbook.

The guidance names the two moments that matter — before doing a recurring task,
and before recording a new procedure so an existing one is updated rather than
duplicated — and is session-stable so it costs nothing per turn.
"""

from __future__ import annotations

from typing import Any

import pytest

from arcagent.core.session_internal.context import _RESERVED_TAGS, _SESSION_SECTIONS
from arcagent.modules.memory import _runtime
from arcagent.modules.memory.capabilities import inject_procedure_guidance


class _Ctx:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {"sections": {}}


@pytest.fixture(autouse=True)
def _clean_runtime() -> Any:
    _runtime.reset()
    yield
    _runtime.reset()


def _configure(tmp_path: Any, *, brain: str) -> None:
    """Bind memory state the way agent startup does. ``brain='none'`` = NullBrain."""
    _runtime.configure(
        config={"brain": brain},
        workspace=tmp_path,
        agent_did="did:arc:test:guidance",
    )


async def test_guidance_names_both_moments_a_procedure_matters(tmp_path: Any) -> None:
    """Following one and updating one are different triggers; both must be named."""
    _configure(tmp_path, brain="arcmemory")
    ctx = _Ctx()
    await inject_procedure_guidance(ctx)

    text = ctx.data["sections"]["procedures"]
    assert "procedure_list" in text and "procedure_get" in text
    assert "duplicat" in text.lower(), "nothing warns against creating a second card"


async def test_no_guidance_when_memory_is_off(tmp_path: Any) -> None:
    """A NullBrain has no procedures, so the tokens would buy nothing."""
    _configure(tmp_path, brain="none")
    ctx = _Ctx()
    await inject_procedure_guidance(ctx)

    assert "procedures" not in ctx.data["sections"]


async def test_the_section_is_session_stable_and_unforgeable() -> None:
    """Two properties that are easy to miss and expensive to get wrong.

    Session-stable keeps the guidance inside the cached prompt prefix instead of
    re-billing it every turn. Reserved keeps untrusted text from forging a
    ``<procedures>`` boundary and dictating which playbook the agent follows.
    """
    assert "procedures" in _SESSION_SECTIONS
    assert "procedures" in _RESERVED_TAGS
