"""SPEC-054 production wiring (RED) — Curator suite sweep + arg forwarding (REQ-107/117).

Pins the arcagent side of the producers-unwired closure:

- ``_runtime.run_lifecycle_sweep`` calls the adapter's ``sweep_suites()`` after
  ``review_lifecycle`` (the sole REQ-107 production producer), and tolerates a BYO
  adapter that predates the method.
- ``SkillAdapter``/``NullSkillAdapter`` gain ``sweep_suites()`` (additive no-op) and
  the optional ``args`` kwarg on ``observe``.
- ``skills_post_tool`` forwards the tool-call args into ``adapter.observe(args=...)``
  ONLY when the adapter's observe accepts the kwarg — the scrub/persist decision
  lives entirely arcskill-side; arcagent just forwards (REQ-117).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from arcagent.modules.skills import _runtime
from arcagent.modules.skills.capabilities import skills_post_tool
from arcagent.skilladapt import NullSkillAdapter


class _Ctx:
    _next_call_id = 0

    def __init__(self, **data: Any) -> None:
        if "tool" in data and "call_id" not in data:
            type(self)._next_call_id += 1
            data["call_id"] = f"test-call-{self._next_call_id}"
        self.data = data
        self.is_vetoed = False


class _SweepAdapter:
    """Records the sweep call order the Curator loop drives."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def review_lifecycle(self, *, turn: int) -> None:
        self.calls.append("review_lifecycle")

    async def sweep_suites(self) -> None:
        self.calls.append("sweep_suites")

    async def review_consolidation(self, *, turn: int) -> None:
        self.calls.append("review_consolidation")

    def retired_skills(self) -> frozenset[str]:
        return frozenset()


class _MinimalAdapter:
    """Records canonical observations for a minimal skill adapter."""

    def __init__(self) -> None:
        self.observations: list[dict[str, Any]] = []
        self.reviews = 0

    async def observe(
        self,
        *,
        skill_name: str,
        tool_name: str,
        status: str,
        error_type: str | None,
        session_id: str | None = None,
        run_id: str | None = None,
        call_id: str = "",
        llm_trace_id: str | None = None,
        args: dict[str, Any] | None = None,
    ) -> None:
        self.observations.append(
            {
                "skill_name": skill_name,
                "tool_name": tool_name,
                "call_id": call_id,
                "run_id": run_id,
                "args": args,
            }
        )

    async def review_lifecycle(self, *, turn: int) -> None:
        self.reviews += 1

    async def sweep_suites(self) -> None:
        return None

    async def review_consolidation(self, *, turn: int) -> None:
        return None

    def retired_skills(self) -> frozenset[str]:
        return frozenset()


class _ArgsAdapter:
    """An adapter whose observe declares the optional args kwarg (REQ-117 shape)."""

    def __init__(self) -> None:
        self.observations: list[dict[str, Any]] = []

    async def observe(
        self,
        *,
        skill_name: str,
        tool_name: str,
        status: str,
        error_type: str | None,
        session_id: str | None = None,
        run_id: str | None = None,
        call_id: str = "",
        llm_trace_id: str | None = None,
        args: dict[str, Any] | None = None,
    ) -> None:
        self.observations.append({"tool_name": tool_name, "args": args})


@pytest.fixture(autouse=True)
def _clean_runtime() -> Any:
    _runtime.reset()
    yield
    _runtime.reset()


def _bind(adapter: Any, workspace: Path, *, active_skill: str | None = None) -> None:
    state = _runtime._State(
        adapter=adapter,  # duck-typed fake records calls
        active=True,
        workspace=workspace,
    )
    state.turn("", "").active_skill = active_skill
    _runtime.bind(state)


# -- REQ-107: the Curator loop is the suite-sweep production producer -----------


@pytest.mark.asyncio
async def test_lifecycle_sweep_calls_sweep_suites_after_review(tmp_path: Path) -> None:
    adapter = _SweepAdapter()
    _bind(adapter, tmp_path)

    await _runtime.run_lifecycle_sweep()

    assert adapter.calls == ["review_lifecycle", "sweep_suites", "review_consolidation"]


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_null_adapter_gains_additive_noop_surface() -> None:
    """Every hook is inert — the call must not raise (a ``-> None`` seam has nothing else
    to assert against, so mypy correctly rejects comparing its result to ``None``)."""
    null = NullSkillAdapter()
    await null.sweep_suites()
    await null.review_consolidation(turn=1)
    await null.observe(
        skill_name="s",
        tool_name="t",
        status="ok",
        error_type=None,
        call_id="test-call",
        args={"x": 1},
    )


# -- REQ-117: post_tool forwards bounded, scrubbed arguments -------------------


@pytest.mark.asyncio
async def test_post_tool_forwards_args_to_accepting_adapter(tmp_path: Path) -> None:
    adapter = _ArgsAdapter()
    _bind(adapter, tmp_path, active_skill="my-skill")

    await skills_post_tool(_Ctx(tool="bash", args={"command": "ls"}))

    assert adapter.observations == [{"tool_name": "bash", "args": {"command": "ls"}}]


@pytest.mark.asyncio
async def test_post_tool_forwards_empty_args_as_none(tmp_path: Path) -> None:
    adapter = _ArgsAdapter()
    _bind(adapter, tmp_path, active_skill="my-skill")

    await skills_post_tool(_Ctx(tool="bash", args={}))

    assert adapter.observations == [{"tool_name": "bash", "args": None}]


@pytest.mark.asyncio
async def test_post_tool_never_forwards_nested_credentials_to_an_adapter(tmp_path: Path) -> None:
    adapter = _ArgsAdapter()
    _bind(adapter, tmp_path, active_skill="my-skill")

    await skills_post_tool(
        _Ctx(tool="http", args={"body": {"client_secret": "hunter2", "query": "safe"}})
    )
    await skills_post_tool(
        _Ctx(tool="http", args={"query": "sk-abcdefghijklmnopqrstuvwxyz123456"})
    )

    assert adapter.observations == [
        {"tool_name": "http", "args": None},
        {"tool_name": "http", "args": None},
    ]


@pytest.mark.asyncio
async def test_post_tool_forwards_canonical_correlation_to_adapter(tmp_path: Path) -> None:
    adapter = _MinimalAdapter()
    _bind(adapter, tmp_path, active_skill="my-skill")
    _runtime.state().turn("", "run-a").active_skill = "my-skill"

    await skills_post_tool(
        _Ctx(
            tool="bash",
            args={"command": "ls"},
            call_id="call-a",
            run_id="run-a",
        )
    )

    assert adapter.observations == [
        {
            "skill_name": "my-skill",
            "tool_name": "bash",
            "call_id": "call-a",
            "run_id": "run-a",
            "args": {"command": "ls"},
        }
    ]
