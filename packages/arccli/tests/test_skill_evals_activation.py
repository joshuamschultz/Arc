"""Item 55: `arc skill evals promote/edit` never leave unsigned eval files on an installed skill."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import arcagent
import pytest
from arcagent.modules.capability_import.archive import intake
from arcagent.modules.capability_import.models import CapabilityImportLimits
from arctrust.policy import OperatorApprovalAuthority

from arccli.commands import _serve
from arccli.commands.operator import resolve_operator_signer
from arccli.commands.skill_evals import evals_handler

_AGENT_DID = "did:arc:agent:ada"
_SKILL = (
    "---\nname: reporter\ndescription: Create reports\n---\n"
    "## Resources\nnone\n## Contract\nfollow the steps\n"
    "## Knowledge\nsource data\n## Steps\ndo it\n"
    "## Anti Patterns\nnone\n## Examples\nexample\n## Validation\ncheck\n"
)
_EVAL = "def test_one():\n    assert 1 == 1\n"


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARC_TEAM_ROOT", raising=False)
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc-home"))
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(tmp_path / "arc-data"))


def _install_skill(tmp_path: Path) -> Path:
    """An operator-promoted, signed skill under an agent home. Returns the installed folder."""
    agent = tmp_path / "agent"
    root = agent / "capabilities"
    root.mkdir(parents=True)
    config = agent / "arcagent.toml"
    config.write_text(
        f'[security]\ntier = "personal"\n[identity]\ndid = "{_AGENT_DID}"\n', encoding="utf-8"
    )
    source = tmp_path / "source"
    folder = source / "skills" / "reporter"
    (folder / "evals").mkdir(parents=True)
    (folder / "SKILL.md").write_text(_SKILL, encoding="utf-8")
    (folder / "evals" / "test_human.py").write_text(_EVAL, encoding="utf-8")
    signer = resolve_operator_signer()
    service = arcagent.CapabilityImportService(root)
    imported = intake(source, root)
    service.review(imported, target_agent_did=_AGENT_DID, limits=CapabilityImportLimits())
    service.promote(
        imported.staging_dir,
        target_agent_did=_AGENT_DID,
        operator_did=OperatorApprovalAuthority(signer).did,
        signer=signer,
        config_path=config,
    )
    return root / "skills" / "reporter"


def _spec(tmp_path: Path) -> Path:
    spec = tmp_path / "spec.json"
    spec.write_text(
        json.dumps(
            {
                "case_id": "invoice",
                "skill_name": "reporter",
                "gate_type": "exact_match",
                "ideal_output": "Acme owes $42",
            }
        ),
        encoding="utf-8",
    )
    return spec


def _resolver(folder: Path) -> arcagent.AnchoredSkillRevisionResolver:
    factory = _serve.build_skill_revision_anchor_factory()
    assert factory is not None
    return arcagent.AnchoredSkillRevisionResolver(
        agent_did=_AGENT_DID,
        config_path=folder.parent.parent.parent / "arcagent.toml",
        anchor_factory=factory,
    )


def _evals(*target: str) -> int:
    args = argparse.Namespace(target=list(target), force=False, yes=False, json=False)
    try:
        evals_handler(args)
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def test_evals_promote_creates_anchored_revision(tmp_path: Path) -> None:
    folder = _install_skill(tmp_path)
    resolver = _resolver(folder)
    assert resolver.active_folder(folder) is None
    before = sorted(p.name for p in folder.rglob("*"))

    code = _evals("promote", str(folder), str(_spec(tmp_path)))

    assert code == 0
    active = resolver.active_folder(folder)
    assert active is not None, "promote must activate an operator-signed anchored revision"
    curated = sorted((active / "evals" / "curated").glob("test_curated_*.py"))
    assert curated and all(Path(f"{path}.arcsig").is_file() for path in curated)
    # The installed original is untouched: no unsigned file landed beside it.
    assert sorted(p.name for p in folder.rglob("*")) == before


def test_evals_edit_without_authority_returns_activation_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = _install_skill(tmp_path)
    monkeypatch.setattr(_serve, "build_skill_revision_anchor_factory", lambda _sink=None: None)
    editor = tmp_path / "append.sh"
    editor.write_text("#!/bin/sh\nprintf 'def test_two():\\n    assert True\\n' >> \"$1\"\n")
    editor.chmod(0o755)
    monkeypatch.setenv("EDITOR", str(editor))
    monkeypatch.delenv("VISUAL", raising=False)
    original = (folder / "evals" / "test_human.py").read_bytes()
    before = sorted(p.name for p in folder.rglob("*"))

    code = _evals("edit", str(folder), "test_human.py")

    assert code != 0
    assert "activation unavailable" in capsys.readouterr().err.lower()
    assert (folder / "evals" / "test_human.py").read_bytes() == original
    assert sorted(p.name for p in folder.rglob("*")) == before
