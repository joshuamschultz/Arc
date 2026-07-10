"""SPEC-054 REQ-105 / COMP-005 — create_skill must not write a placeholder suite.

Vulnerability being closed: the current scaffold writes ``evals/test_golden.py``
containing a ``test_placeholder`` whose body is ``assert True``. arcskill's
``load_suite`` counts any ``test_*`` function, so the placeholder makes a fresh
skill's suite non-empty and ``EvalGate.decide`` never reaches the fail-closed
``no_suite_policy`` — silently bypassing the enterprise/federal no-suite block.

Post-fix behavior asserted here: ``create_skill`` leaves ``evals/`` empty, so a
freshly created skill has an empty suite and ``no_suite_policy`` governs from
birth (enterprise/federal prose blocked, personal prose audit-warn accepted),
and no code path ever writes an ``assert True``-only test body.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arcskill.improver.evalgate import load_suite, no_suite_policy

from arcagent.builtins.capabilities import _runtime
from arcagent.capabilities.capability_loader import CapabilityLoader
from arcagent.capabilities.capability_registry import CapabilityRegistry


@pytest.fixture(autouse=True)
def _reset_runtime() -> None:
    _runtime.reset()


@pytest.fixture
def configured(tmp_path: Path) -> Path:
    """Workspace + loader configured; capabilities/ subdir present."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "capabilities").mkdir()
    reg = CapabilityRegistry()
    loader = CapabilityLoader(
        scan_roots=[("workspace", workspace / "capabilities")],
        registry=reg,
    )
    _runtime.configure(workspace=workspace, loader=loader)
    return workspace


async def _scaffold(configured: Path) -> Path:
    """Create a fresh skill and return its folder."""
    from arcagent.builtins.capabilities.create_skill import create_skill

    result = await create_skill(
        name="fresh-skill",
        description="does X",
        triggers=["do x"],
        tools=["read"],
    )
    assert "Created skill 'fresh-skill'" in result
    return configured / "capabilities/skills" / "fresh-skill"


@pytest.mark.asyncio
class TestCreateSkillEmptyEvals:
    async def test_evals_dir_created_empty(self, configured: Path) -> None:
        folder = await _scaffold(configured)
        evals_dir = folder / "evals"
        assert evals_dir.is_dir()
        assert list(evals_dir.iterdir()) == []

    @pytest.mark.parametrize(
        ("tier", "accepted"),
        [("enterprise", False), ("federal", False), ("personal", True)],
    )
    async def test_no_suite_policy_governs_from_birth(
        self, configured: Path, tier: str, accepted: bool
    ) -> None:
        folder = await _scaffold(configured)
        # The placeholder bypass: a non-empty suite here means EvalGate.decide
        # never consults no_suite_policy for a freshly scaffolded skill.
        assert load_suite(folder) == []
        assert no_suite_policy(tier, "prose").accepted is accepted

    async def test_no_assert_true_placeholder_anywhere(self, configured: Path) -> None:
        folder = await _scaffold(configured)
        offenders = [
            path.relative_to(folder).as_posix()
            for path in sorted(folder.rglob("*"))
            if path.is_file()
            and "assert True" in path.read_text(encoding="utf-8", errors="ignore")
        ]
        assert offenders == []
