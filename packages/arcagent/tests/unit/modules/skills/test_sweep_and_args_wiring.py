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
    def __init__(self, **data: Any) -> None:
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

    def retired_skills(self) -> frozenset[str]:
        return frozenset()


class _LegacyAdapter:
    """A pre-SPEC-054 adapter: no sweep_suites, observe without the args kwarg."""

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
    ) -> None:
        self.observations.append({"skill_name": skill_name, "tool_name": tool_name})

    async def review_lifecycle(self, *, turn: int) -> None:
        self.reviews += 1

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
    state.active_skill = active_skill
    _runtime.bind(state)


# -- REQ-107: the Curator loop is the suite-sweep production producer -----------


@pytest.mark.asyncio
async def test_lifecycle_sweep_calls_sweep_suites_after_review(tmp_path: Path) -> None:
    adapter = _SweepAdapter()
    _bind(adapter, tmp_path)

    await _runtime.run_lifecycle_sweep()

    assert adapter.calls == ["review_lifecycle", "sweep_suites"]


@pytest.mark.asyncio
async def test_lifecycle_sweep_tolerates_adapter_without_sweep_suites(tmp_path: Path) -> None:
    adapter = _LegacyAdapter()
    _bind(adapter, tmp_path)

    await _runtime.run_lifecycle_sweep()

    assert adapter.reviews == 1


@pytest.mark.asyncio
async def test_null_adapter_gains_additive_noop_surface() -> None:
    null = NullSkillAdapter()
    assert await null.sweep_suites() is None
    assert (
        await null.observe(
            skill_name="s", tool_name="t", status="ok", error_type=None, args={"x": 1}
        )
        is None
    )


# -- REQ-117: post_tool forwards args only to adapters that accept them ---------


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
async def test_post_tool_omits_args_for_legacy_adapter(tmp_path: Path) -> None:
    """A BYO adapter without the args kwarg keeps working — no TypeError, no args."""
    adapter = _LegacyAdapter()
    _bind(adapter, tmp_path, active_skill="my-skill")

    await skills_post_tool(_Ctx(tool="bash", args={"command": "ls"}))

    assert adapter.observations == [{"skill_name": "my-skill", "tool_name": "bash"}]
