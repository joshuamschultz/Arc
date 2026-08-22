"""Promotion is an explicit, signed, agent-scoped trust mutation."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from arctrust import AuditEvent, InProcessSigner, generate_keypair, load_validators

import arcagent.modules.capability_import.service as capability_service
from arcagent.modules.capability_import.archive import intake
from arcagent.modules.capability_import.models import (
    CapabilityImportLimits,
    CapabilityImportManifest,
    CapabilityImportStatus,
)
from arcagent.modules.capability_import.service import CapabilityImportService

_DID = "did:arc:operator:alpha"
_CONFIG = """\
[agent]
name = "promotion-test"

[security]
tier = "federal"

[security.validators]
auto_run_agent_code = false
"""
_TOOL = b"from arcagent import tool\n@tool(description='ok', version='1.0.0')\nasync def imported_tool() -> str:\n    return 'ok'\n"
_SKILL = b"""---
name: imported_skill
version: 1.0.0
description: imported skill
triggers: [imported]
tools: [reload]
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


class _Sink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


def _archive(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("tools/imported_tool.py", _TOOL)
        archive.writestr("skills/imported_skill/SKILL.md", _SKILL)
    return path


def _setup(tmp_path: Path) -> tuple[Path, Path, CapabilityImportService, CapabilityImportManifest]:
    agent = tmp_path / "agent"
    capabilities = agent / "capabilities"
    capabilities.mkdir(parents=True)
    config = agent / "arcagent.toml"
    config.write_text(_CONFIG, encoding="utf-8")
    staged = intake(_archive(tmp_path / "import.zip"), capabilities)
    service = CapabilityImportService(capabilities)
    manifest = service.review(
        staged,
        target_agent_did="did:arc:agent:target",
        limits=CapabilityImportLimits(),
    )
    return config, staged.staging_dir, service, manifest


def test_promotion_requires_reviewed_unchanged_staging_and_signs_agent_files(
    tmp_path: Path,
) -> None:
    config, staging, service, manifest = _setup(tmp_path)
    key = generate_keypair()
    sink = _Sink()

    promoted = service.promote(
        staging,
        target_agent_did="did:arc:agent:target",
        operator_did=_DID,
        signer=InProcessSigner(key.private_key),
        config_path=config,
        audit_sink=sink,
    )

    assert {
        path.relative_to(tmp_path / "agent" / "capabilities").as_posix() for path in promoted
    } == {
        "imported_tool.py",
        "skills/imported_skill/SKILL.md",
    }
    assert (tmp_path / "agent" / "capabilities" / "imported_tool.py.arcsig").is_file()
    assert (
        tmp_path / "agent" / "capabilities" / "skills/imported_skill/SKILL.md.arcsig"
    ).is_file()
    assert service.status(manifest, staging) is CapabilityImportStatus.PROMOTED
    assert sink.events[-1].action == "capability_import.promoted"
    assert key.public_key.hex() in config.read_text(encoding="utf-8")
    assert {entry.name for entry in load_validators(config).approved} == {
        "imported_tool",
        "imported_skill",
    }


def test_revoke_removes_promoted_files_and_trust_and_is_audited(tmp_path: Path) -> None:
    config, staging, service, manifest = _setup(tmp_path)
    key = generate_keypair()
    sink = _Sink()
    service.promote(
        staging,
        target_agent_did="did:arc:agent:target",
        operator_did=_DID,
        signer=InProcessSigner(key.private_key),
        config_path=config,
        audit_sink=sink,
    )

    service.revoke(
        staging,
        operator_did=_DID,
        config_path=config,
        audit_sink=sink,
    )

    capabilities = tmp_path / "agent" / "capabilities"
    assert not (capabilities / "imported_tool.py").exists()
    assert not (capabilities / "imported_tool.py.arcsig").exists()
    assert not (capabilities / "skills" / "imported_skill").exists()
    assert service.status(manifest, staging) is CapabilityImportStatus.REVOKED
    assert sink.events[-1].action == "capability_import.revoked"
    assert key.public_key.hex() not in config.read_text(encoding="utf-8")
    assert load_validators(config).approved == ()


def test_promotion_rejects_modified_staging_without_writing_capabilities(tmp_path: Path) -> None:
    config, staging, service, manifest = _setup(tmp_path)
    (staging / "tools" / "imported_tool.py").write_bytes(b"changed")

    try:
        service.promote(
            staging,
            target_agent_did=manifest.target_agent_did,
            operator_did=_DID,
            signer=InProcessSigner(generate_keypair().private_key),
            config_path=config,
        )
    except ValueError as exc:
        assert "review" in str(exc)
    else:
        raise AssertionError("modified staged content was promoted")
    assert not (tmp_path / "agent" / "capabilities" / "imported_tool.py").exists()


def test_promotion_rejects_tampered_review_manifest(tmp_path: Path) -> None:
    config, staging, service, manifest = _setup(tmp_path)
    manifest_path = staging / "import.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["tools"] = ["unreviewed_tool"]
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="manifest"):
        service.promote(
            staging,
            target_agent_did=manifest.target_agent_did,
            operator_did=_DID,
            signer=InProcessSigner(generate_keypair().private_key),
            config_path=config,
        )
    assert not (tmp_path / "agent" / "capabilities" / "imported_tool.py").exists()


def test_promotion_rolls_back_trust_when_approval_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, staging, service, manifest = _setup(tmp_path)
    key = generate_keypair()
    original_approve = capability_service.approve
    calls = 0

    def fail_second_approval(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated config failure")
        return original_approve(*args, **kwargs)

    monkeypatch.setattr(capability_service, "approve", fail_second_approval)
    with pytest.raises(OSError, match="config"):
        service.promote(
            staging,
            target_agent_did=manifest.target_agent_did,
            operator_did=_DID,
            signer=InProcessSigner(key.private_key),
            config_path=config,
        )

    capabilities = tmp_path / "agent" / "capabilities"
    assert not (capabilities / "imported_tool.py").exists()
    assert not (capabilities / "skills" / "imported_skill" / "SKILL.md").exists()
    assert load_validators(config).approved == ()
    assert key.public_key.hex() not in config.read_text(encoding="utf-8")


def test_promotion_rejects_symlinked_capability_parent(tmp_path: Path) -> None:
    config, staging, service, manifest = _setup(tmp_path)
    capabilities = tmp_path / "agent" / "capabilities"
    outside = tmp_path / "outside"
    outside.mkdir()
    (capabilities / "skills").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        service.promote(
            staging,
            target_agent_did=manifest.target_agent_did,
            operator_did=_DID,
            signer=InProcessSigner(generate_keypair().private_key),
            config_path=config,
        )
    assert not (outside / "imported_skill" / "SKILL.md").exists()


def test_promotion_restores_review_row_when_promoted_ledger_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, staging, service, manifest = _setup(tmp_path)
    original_set = service._ledger.set

    def fail_promoted(import_id, status, **data):
        row = original_set(import_id, status, **data)
        if status is CapabilityImportStatus.PROMOTED:
            raise OSError("simulated ledger failure")
        return row

    monkeypatch.setattr(service._ledger, "set", fail_promoted)
    with pytest.raises(OSError, match="ledger"):
        service.promote(
            staging,
            target_agent_did=manifest.target_agent_did,
            operator_did=_DID,
            signer=InProcessSigner(generate_keypair().private_key),
            config_path=config,
        )

    capabilities = tmp_path / "agent" / "capabilities"
    assert not (capabilities / "imported_tool.py").exists()
    assert load_validators(config).approved == ()
    assert service._ledger.get(manifest.import_id) == {
        "status": CapabilityImportStatus.REVIEW_READY.value,
        "review_digest": manifest.review_digest,
        "target_agent_did": manifest.target_agent_did,
    }


def test_revoke_restores_exact_state_when_second_artifact_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, staging, service, manifest = _setup(tmp_path)
    key = generate_keypair()
    promoted = service.promote(
        staging,
        target_agent_did=manifest.target_agent_did,
        operator_did=_DID,
        signer=InProcessSigner(key.private_key),
        config_path=config,
    )
    config_before = config.read_text(encoding="utf-8")
    original_revoke = capability_service.revoke_capability
    calls = 0

    def fail_second(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated revoke failure")
        original_revoke(*args, **kwargs)

    monkeypatch.setattr(capability_service, "revoke_capability", fail_second)
    with pytest.raises(OSError, match="revoke"):
        service.revoke(staging, operator_did=_DID, config_path=config)

    for path in promoted:
        assert path.is_file()
        assert path.with_name(path.name + ".arcsig").is_file()
    assert config.read_text(encoding="utf-8") == config_before
    assert service._ledger.get(manifest.import_id) == {
        "status": CapabilityImportStatus.PROMOTED.value,
        "target_agent_did": manifest.target_agent_did,
        "promoted_paths": [
            path.relative_to(tmp_path / "agent" / "capabilities").as_posix() for path in promoted
        ],
    }
