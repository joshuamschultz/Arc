"""CLI behavior for agent-local capability-import reviews."""

from __future__ import annotations

import zipfile
from pathlib import Path

from arcagent.modules.capability_import.archive import intake
from arcagent.modules.capability_import.models import CapabilityImportLimits
from arcagent.modules.capability_import.service import CapabilityImportService
from arctrust import OperatorKey, default_operator_key_path

from arccli.commands import capability_import as command

_SKILL = b"""---
name: imported
version: 1.0.0
description: imported skill
triggers: [imported]
tools: []
---
## Resources
none
## Contract
none
## Knowledge
none
## Steps
Use it.
## Anti Patterns
none
## Examples
none
## Validation
none
"""


def _setup(tmp_path: Path) -> tuple[Path, str]:
    agent_root = tmp_path / "agent"
    capabilities = agent_root / "capabilities"
    capabilities.mkdir(parents=True)
    (agent_root / "arcagent.toml").write_text(
        '[agent]\nname = "test"\n\n[security]\ntier = "federal"\n', encoding="utf-8"
    )
    archive = tmp_path / "import.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("skills/imported/SKILL.md", _SKILL)
    result = intake(archive, capabilities)
    service = CapabilityImportService(capabilities)
    manifest = service.review(
        result,
        target_agent_did="did:arc:agent:test",
        limits=CapabilityImportLimits(),
    )
    return agent_root, manifest.import_id


def test_promote_and_revoke_use_the_deployment_operator_signer(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc"))
    operator = OperatorKey.load(default_operator_key_path(), generate_if_absent=True)
    agent_root, import_id = _setup(tmp_path)
    monkeypatch.setattr(
        command,
        "_resolve_target",
        lambda _agent: ("ada", agent_root, "did:arc:agent:test"),
    )

    command.capability_import_handler(["promote", import_id])
    assert "Promoted" in capsys.readouterr().out
    assert (agent_root / "capabilities" / "skills" / "imported" / "SKILL.md.arcsig").is_file()
    assert operator.public_key.hex() in (agent_root / "arcagent.toml").read_text(encoding="utf-8")

    command.capability_import_handler(["revoke", import_id])
    assert "Revoked" in capsys.readouterr().out
    assert not (agent_root / "capabilities" / "skills" / "imported" / "SKILL.md").exists()


def test_list_and_edit_are_agent_scoped_and_review_bound(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    agent_root, import_id = _setup(tmp_path)
    monkeypatch.setattr(
        command,
        "_resolve_target",
        lambda _agent: ("ada", agent_root, "did:arc:agent:test"),
    )

    command.capability_import_handler(["list"])
    assert import_id in capsys.readouterr().out

    content_file = tmp_path / "edited.md"
    content_file.write_bytes(_SKILL.replace(b"Use it.", b"Use edited."))
    command.capability_import_handler(
        [
            "edit",
            import_id,
            "skills/imported/SKILL.md",
            "--content-file",
            str(content_file),
        ]
    )
    assert "review digest" in capsys.readouterr().out
    staged = agent_root / "capabilities/imports/.staging" / import_id / "skills/imported/SKILL.md"
    assert b"Use edited." in staged.read_bytes()
