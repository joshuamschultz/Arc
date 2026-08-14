"""Every skill a blueprint ships must pass the validator the agent gates on.

A blueprint's skills are its actual behaviour. The four domain blueprints used to
carry that behaviour as Python capabilities instead, which could never run — they
sat in the contained ``capabilities/`` root importing agent internals, and their
unit tests passed by importing the file directly and calling
``_runtime.configure(workspace=tmp_path)``, the one environment the capability
never got. The behaviour now lives in skills, so the thing worth checking is that
those skills actually load rather than that some Python parses.

A skill failing ``skill_validator`` is gated at load: the agent silently runs
without it, exactly the dead-on-arrival state this replaced.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arcagent.capabilities.skill_validator import validate_skill_folder

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SKILLS = sorted((_REPO_ROOT / "blueprints").glob("*/skills/*/SKILL.md"))


def test_the_blueprints_ship_skills_at_all() -> None:
    """Guard the guard: an empty glob would make every check below vacuous."""
    assert len(_SKILLS) >= 4, f"expected blueprint skills, found {_SKILLS}"


@pytest.mark.parametrize("skill_md", _SKILLS, ids=lambda p: f"{p.parents[2].name}/{p.parent.name}")
def test_a_shipped_skill_validates(skill_md: Path) -> None:
    """One case per skill, so a failure names the blueprint to fix."""
    result = validate_skill_folder(skill_md.parent, scan_root="blueprint")
    assert result.ok, (
        f"{skill_md.relative_to(_REPO_ROOT)} is gated at load and the agent would "
        f"run without it: {[error.message for error in result.errors]}"
    )
