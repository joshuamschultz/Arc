"""SPEC-044 Phase 2 — SkillAdapter selection + BYO signing gate (REQ-003)."""

from __future__ import annotations

from pathlib import Path

import pytest

from arcagent.skilladapt import NullSkillAdapter
from arcagent.skilladapt.select import select_skill_adapter


def test_none_selects_null_adapter(tmp_path: Path) -> None:
    assert isinstance(select_skill_adapter("none", workspace=tmp_path), NullSkillAdapter)
    assert isinstance(select_skill_adapter("", workspace=tmp_path), NullSkillAdapter)


def test_arcskill_selects_real_improver(tmp_path: Path) -> None:
    from arcskill.improver import ArcSkillImprover

    adapter = select_skill_adapter("arcskill", workspace=tmp_path, tier="federal")
    assert isinstance(adapter, ArcSkillImprover)
    assert adapter.tier == "federal"


def test_byo_unsigned_refused_at_enterprise(tmp_path: Path) -> None:
    """A dotted BYO class-path not on the allowlist is refused above personal (fail-closed)."""
    with pytest.raises(ValueError, match="not on the operator allowlist"):
        select_skill_adapter("evil.mod:Adapter", workspace=tmp_path, tier="enterprise")


def test_byo_unsigned_refused_at_federal(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="fail-closed"):
        select_skill_adapter("evil.mod:Adapter", workspace=tmp_path, tier="federal")


def test_byo_allowlisted_is_imported_above_personal(tmp_path: Path) -> None:
    """An allowlisted BYO class-path is imported and instantiated ``cls(workspace)``."""
    from arcskill.improver import ArcSkillImprover

    path = "arcskill.improver:ArcSkillImprover"
    adapter = select_skill_adapter(
        path, workspace=tmp_path, tier="federal", adapter_allowlist=(path,)
    )
    assert isinstance(adapter, ArcSkillImprover)


def test_byo_personal_allowed_without_allowlist(tmp_path: Path) -> None:
    """Personal tier may load a BYO adapter without allowlisting (audit-warn posture)."""
    from arcskill.improver import ArcSkillImprover

    adapter = select_skill_adapter(
        "arcskill.improver:ArcSkillImprover", workspace=tmp_path, tier="personal"
    )
    assert isinstance(adapter, ArcSkillImprover)
