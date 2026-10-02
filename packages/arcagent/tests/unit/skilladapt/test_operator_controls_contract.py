"""alpha-2 P8 — the operator controls on the ``SkillAdapter`` seam (contract test).

``improve_now`` / ``run_evals`` / ``regen_evals`` are part of the structural
Protocol, so every implementation — the default :class:`NullSkillAdapter` and the
arcskill ``ArcSkillImprover`` — answers them with the same result shape: a dict
carrying ``status``, ``skill_name`` and ``reason``. The Null adapter answers
``unavailable`` and writes nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arcskill.improver import ArcSkillImprover

from arcagent.skilladapt import NullSkillAdapter, SkillAdapter


def _null(tmp_path: Path) -> Any:
    return NullSkillAdapter()


def _arcskill(tmp_path: Path) -> Any:
    return ArcSkillImprover(tmp_path / "ws", skill_path=lambda name: None)


_IMPLEMENTATIONS = [pytest.param(_null, id="null"), pytest.param(_arcskill, id="arcskill")]


@pytest.mark.parametrize("make", _IMPLEMENTATIONS)
def test_implementation_satisfies_the_protocol(make: Any, tmp_path: Path) -> None:
    assert isinstance(make(tmp_path), SkillAdapter)


@pytest.mark.parametrize("make", _IMPLEMENTATIONS)
@pytest.mark.asyncio
async def test_every_control_returns_the_shared_result_shape(make: Any, tmp_path: Path) -> None:
    adapter: SkillAdapter = make(tmp_path)
    results = [
        await adapter.improve_now(skill_name="s", dry_run=True),
        await adapter.improve_now(skill_name="s", dry_run=False, preview_id="p"),
        await adapter.run_evals(skill_name="s"),
        await adapter.regen_evals(skill_name="s"),
    ]
    for result in results:
        assert {"status", "skill_name", "reason"} <= set(result)
        assert result["skill_name"] == "s"
        assert isinstance(result["status"], str)


@pytest.mark.asyncio
async def test_null_adapter_controls_are_unavailable_and_write_nothing(tmp_path: Path) -> None:
    adapter = NullSkillAdapter()
    for result in (
        await adapter.improve_now(skill_name="s", dry_run=False),
        await adapter.run_evals(skill_name="s"),
        await adapter.regen_evals(skill_name="s"),
    ):
        assert result["status"] == "unavailable"
        assert result["reason"]
    assert not any(tmp_path.rglob("*"))
