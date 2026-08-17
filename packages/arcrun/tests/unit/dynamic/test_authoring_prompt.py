"""The prompt that teaches the language must itself speak it.

An example the model is shown but the grammar rejects is worse than no example:
it trains the model to write scripts that cannot run. So every fenced block in
the authoring prompt is parsed here, and the worked example is executed against
a stub host to prove it reaches a terminal state.
"""

from __future__ import annotations

import re
from typing import Any

from arcprompt import load_stock

from arcrun.dynamic.grammar import parse_script
from arcrun.dynamic.host import AgentOutcome, AgentSpec, BudgetState
from arcrun.dynamic.interpreter import execute_script

_FENCE = re.compile(r"^```(?:\w+)?\n(.*?)^```", re.MULTILINE | re.DOTALL)


def _code_blocks() -> list[str]:
    return [block for block in _FENCE.findall(load_stock("arcrun", "dynamic_authoring"))]


class _StubHost:
    """Answers every call plausibly so an example can run end to end."""

    def __init__(self) -> None:
        self.spawned = 0

    async def spawn(self, spec: AgentSpec) -> AgentOutcome:
        self.spawned += 1
        return AgentOutcome(
            agent_id=f"stub-{self.spawned}",
            success=True,
            output={"questions": ["one", "two"], "claim": "a finding"},
            tokens_used=1,
        )

    async def spawn_many(self, specs: list[AgentSpec]) -> list[AgentOutcome]:
        return [await self.spawn(spec) for spec in specs]

    def phase(self, title: str) -> None:
        return None

    def log(self, message: str) -> None:
        return None

    def budget(self) -> BudgetState:
        return BudgetState(agent_calls_spent=self.spawned, agent_calls_total=32)

    def scratch_write(self, name: str, content: str) -> str:
        return name

    def scratch_read(self, name: str) -> str:
        return ""


def test_the_prompt_actually_contains_examples() -> None:
    """Guards the regex: a silent zero would make every other test vacuous."""
    assert len(_code_blocks()) >= 2


def test_every_example_in_the_authoring_prompt_parses() -> None:
    """Whatever we teach must be inside the frozen grammar."""
    for block in _code_blocks():
        parse_script(block)


async def test_the_worked_example_runs_to_a_terminal_state() -> None:
    """Parsing is not enough — the example must also produce an answer."""
    example = max(_code_blocks(), key=len)
    host = _StubHost()
    outcome: Any = await execute_script(
        example, host=host, args={"task": "why do batteries degrade"}
    )
    assert outcome.status == "completed", outcome.error
    assert host.spawned > 1
    assert outcome.phases_seen == ["plan", "work", "finish"]
