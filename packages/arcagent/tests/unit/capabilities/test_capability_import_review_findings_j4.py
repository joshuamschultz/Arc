"""J4 M4 / G10 — review findings reach the review wire contract.

``build_manifest`` already computes findings (``builtin_name_collision``) but the
metadata-only review contract the UI and CLI read dropped them, so an operator
could promote a shadowing skill without ever being told. The summary returned by
upload and every row from ``list_reviews`` must carry them.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from arcagent.modules.capability_import.archive import intake
from arcagent.modules.capability_import.models import (
    CapabilityImportLimits,
    CapabilityImportReview,
    CapabilityImportStatus,
)
from arcagent.modules.capability_import.service import CapabilityImportService

_TARGET_DID = "did:arc:agent:target"
_BUILTIN_SKILL_NAME = "create-skill"
# Arc sections keep this fixture valid under the strict and the relaxed validator.
_SECTIONS = (
    "## Files\nnone\n## Contract\nnone\n## Knowledge\nnone\n## Steps\nUse it.\n"
    "## Red Flags\nnone\n## Validation\nnone\n## Examples\nnone\n"
)


def _stage(tmp_path: Path, name: str):
    archive = tmp_path / f"{name}.zip"
    body = f"---\nname: {name}\ndescription: under review\n---\n" + _SECTIONS
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(f"skills/{name}/SKILL.md", body)
    return intake(archive, tmp_path / "agent" / "capabilities")


def test_review_summary_carries_findings(tmp_path: Path) -> None:
    staged = _stage(tmp_path, _BUILTIN_SKILL_NAME)
    service = CapabilityImportService(tmp_path / "agent" / "capabilities")
    manifest = service.review(
        staged, target_agent_did=_TARGET_DID, limits=CapabilityImportLimits()
    )

    summary = service.review_summary(manifest)

    assert f"builtin_name_collision: {_BUILTIN_SKILL_NAME}" in summary.findings
    assert summary.model_dump(mode="json")["findings"] == list(manifest.findings)


def test_listed_review_carries_findings(tmp_path: Path) -> None:
    staged = _stage(tmp_path, _BUILTIN_SKILL_NAME)
    service = CapabilityImportService(tmp_path / "agent" / "capabilities")
    service.review(staged, target_agent_did=_TARGET_DID, limits=CapabilityImportLimits())

    (row,) = service.list_reviews()

    assert f"builtin_name_collision: {_BUILTIN_SKILL_NAME}" in row.findings


def test_review_contract_bounds_each_finding() -> None:
    """Findings are reviewer-visible text from untrusted input; they stay bounded."""
    base = {
        "import_id": "a" * 64,
        "status": CapabilityImportStatus.REVIEW_READY,
        "target_agent_did": _TARGET_DID,
        "archive_sha256": "b" * 64,
        "review_digest": "c" * 64,
        "files": [],
        "tools": [],
        "skills": [],
        "supplier_metadata_keys": [],
        "activation": "review_only",
    }
    with pytest.raises(ValidationError):
        CapabilityImportReview.model_validate({**base, "findings": ["x" * 4097]})
    assert CapabilityImportReview.model_validate(base).findings == []
