"""CLI behavior for agent-local capability-import reviews."""

from __future__ import annotations

import json
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from arcagent.modules.capability_import.archive import intake
from arcagent.modules.capability_import.models import CapabilityImportLimits
from arcagent.modules.capability_import.service import CapabilityImportService
from arctrust import OperatorKey, default_operator_key_path

from arccli.commands import capability_import as command
from arccli.commands.registry import resolve_command_and_args

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

_TOOL = b"""from arcagent import tool


@tool(description="imported", version="1.0.0")
async def imported_tool() -> str:
    return "ok"
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


def _archive(path: Path, entries: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, content in entries.items():
            bundle.writestr(name, content)
    return path


def _agent_root(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    (root / "capabilities").mkdir(parents=True)
    (root / "arcagent.toml").write_text(
        f'[agent]\nname = "{name}"\n\n[security]\ntier = "federal"\n', encoding="utf-8"
    )
    return root


def test_import_stages_tool_and_skill_for_exact_agent_without_activation(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    ada = _agent_root(tmp_path, "ada")
    bea = _agent_root(tmp_path, "bea")
    archive = _archive(
        tmp_path / "capabilities.zip",
        {"tools/imported_tool.py": _TOOL, "skills/imported/SKILL.md": _SKILL},
    )
    resolved: list[str | None] = []

    def resolve(agent: str | None) -> tuple[str, Path, str]:
        resolved.append(agent)
        if agent == "ada":
            return "ada", ada, "did:arc:agent:ada"
        return "bea", bea, "did:arc:agent:bea"

    monkeypatch.setattr(command, "_resolve_target", resolve)

    command.capability_import_handler(["import", str(archive), "--agent", "ada", "--json"])

    payload = json.loads(capsys.readouterr().out)
    import_id = payload["import_id"]
    assert resolved == ["ada"]
    assert payload["agent_id"] == "ada"
    assert payload["target_agent_did"] == "did:arc:agent:ada"
    assert payload["status"] == "review_ready"
    assert payload["activation"] == "review_only"
    assert payload["tools"] == ["imported_tool"]
    assert payload["skills"] == ["imported"]
    assert (ada / "capabilities/imports/.staging" / import_id / "tools/imported_tool.py").is_file()
    assert (ada / "capabilities/imports/.staging" / import_id / "skills/imported/SKILL.md").is_file()
    assert not (ada / "capabilities/tools/imported_tool.py").exists()
    assert not (bea / "capabilities/imports/.staging").exists()


def test_capability_alias_accepts_a_bounded_zip_from_stdin(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    agent_root = _agent_root(tmp_path, "ada")
    archive = _archive(tmp_path / "source.zip", {"skills/imported/SKILL.md": _SKILL})
    monkeypatch.setattr(
        command,
        "_resolve_target",
        lambda _agent: ("ada", agent_root, "did:arc:agent:ada"),
    )
    monkeypatch.setattr(command.sys, "stdin", type("Input", (), {"buffer": BytesIO(archive.read_bytes())})())

    resolved, args = resolve_command_and_args(["capability", "import", "-", "--json"])
    assert resolved is not None
    assert resolved.name == "capability-import"
    resolved.handler(args)  # type: ignore[misc]  # handler proved non-None above

    payload = json.loads(capsys.readouterr().out)
    assert payload["agent_id"] == "ada"
    assert payload["activation"] == "review_only"
    assert (agent_root / "capabilities/imports/.staging" / payload["import_id"]).is_dir()


def test_import_rejects_hostile_archives_before_any_agent_staging(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    agent_root = _agent_root(tmp_path, "ada")
    monkeypatch.setattr(
        command,
        "_resolve_target",
        lambda _agent: ("ada", agent_root, "did:arc:agent:ada"),
    )
    archives = {
        "traversal.zip": {"../tools/escape.py": _TOOL, "skills/imported/SKILL.md": _SKILL},
        "bomb.zip": {"tools/imported_tool.py": b"x" * (8 * 1024 * 1024)},
        "nested.zip": {"skills/imported/SKILL.md": _SKILL, "tools/payload.zip": b"zip"},
    }

    for name, entries in archives.items():
        with pytest.raises(SystemExit, match="1"):
            command.capability_import_handler(["import", str(_archive(tmp_path / name, entries))])
        captured = capsys.readouterr()
        assert "arc capability-import:" in captured.err
        assert not (agent_root / "capabilities/imports/.staging").exists()


def test_import_then_show_edit_and_promote_preserves_review_gate(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc"))
    OperatorKey.load(default_operator_key_path(), generate_if_absent=True)
    agent_root = _agent_root(tmp_path, "ada")
    archive = _archive(tmp_path / "capabilities.zip", {"skills/imported/SKILL.md": _SKILL})
    monkeypatch.setattr(
        command,
        "_resolve_target",
        lambda _agent: ("ada", agent_root, "did:arc:agent:ada"),
    )

    command.capability_import_handler(["import", str(archive), "--json"])
    import_id = json.loads(capsys.readouterr().out)["import_id"]
    assert not (agent_root / "capabilities/skills/imported/SKILL.md").exists()

    command.capability_import_handler(["show", import_id, "skills/imported/SKILL.md"])
    assert b"Use it." in capsys.readouterr().out.encode()

    edited = tmp_path / "edited.md"
    edited.write_bytes(_SKILL.replace(b"Use it.", b"Use edited."))
    command.capability_import_handler(
        ["edit", import_id, "skills/imported/SKILL.md", "--content-file", str(edited)]
    )
    assert "review digest" in capsys.readouterr().out
    assert not (agent_root / "capabilities/skills/imported/SKILL.md").exists()

    command.capability_import_handler(["promote", import_id])
    assert "Promoted" in capsys.readouterr().out
    assert (agent_root / "capabilities/skills/imported/SKILL.md").is_file()


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
