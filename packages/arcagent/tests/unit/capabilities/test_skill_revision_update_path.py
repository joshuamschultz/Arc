"""Zero-config revision authority: unenrolled skills, and importing a new version.

Importing v2 of a skill that is already promoted must not fail with "already
exists": it becomes a new signed revision in the anchored lineage, the original
is enrolled first so it stays reachable, and rollback re-activates it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from arctrust import FileJournalAnchor, InProcessSigner

from arcagent.modules.capability_import.archive import intake
from arcagent.modules.capability_import.models import CapabilityImportLimits
from arcagent.modules.capability_import.revisions import (
    AnchoredSkillRevisionResolver,
    skill_revision_scope,
)
from arcagent.modules.capability_import.service import CapabilityImportService

_AGENT = "did:arc:agent:reporter"
_OPERATOR = "did:arc:operator:test"


def _body(step: str) -> bytes:
    return (
        "---\nname: reporter\ndescription: Create reports\n---\n"
        "## Resources\nnone\n## Contract\nfollow the steps\n"
        "## Knowledge\nsource data\n## Steps\n"
        f"{step}\n"
        "## Anti Patterns\nnone\n## Examples\nexample\n## Validation\ncheck\n"
    ).encode()


def _resolver(
    tmp_path: Path, config: Path, signer: InProcessSigner
) -> AnchoredSkillRevisionResolver:
    anchors = tmp_path / "anchors"
    return AnchoredSkillRevisionResolver(
        agent_did=_AGENT,
        config_path=config,
        anchor_factory=lambda did, name: FileJournalAnchor(
            anchors, scope=skill_revision_scope(did, name), signer=signer
        ),
    )


def _import(
    tmp_path: Path,
    service: CapabilityImportService,
    root: Path,
    label: str,
    body: bytes,
    reference: bytes,
    extra: dict[str, bytes] | None = None,
) -> Path:
    source = tmp_path / f"source-{label}"
    folder = source / "skills" / "reporter"
    (folder / "references").mkdir(parents=True)
    (folder / "SKILL.md").write_bytes(body)
    (folder / "references" / "schema.txt").write_bytes(reference)
    for relative, content in (extra or {}).items():
        (folder / relative).parent.mkdir(parents=True, exist_ok=True)
        (folder / relative).write_bytes(content)
    imported = intake(source, root)
    service.review(imported, target_agent_did=_AGENT, limits=CapabilityImportLimits())
    return imported.staging_dir


def _setup(tmp_path: Path, tier: str = "personal") -> tuple[Path, Path, InProcessSigner]:
    root = tmp_path / "agent" / "capabilities"
    root.mkdir(parents=True)
    config = tmp_path / "agent" / "arcagent.toml"
    config.write_text(f'[security]\ntier = "{tier}"\n')
    return root, config, InProcessSigner(bytes(range(32)))


def test_scope_helper_matches_the_resolver_binding() -> None:
    digest = hashlib.sha256(_AGENT.encode()).hexdigest()
    assert skill_revision_scope(_AGENT, "reporter") == f"skill/{digest}/reporter"


def test_unenrolled_skill_loads_directly_below_federal(tmp_path: Path) -> None:
    root, config, signer = _setup(tmp_path)
    staging = _import(tmp_path, CapabilityImportService(root), root, "v1", _body("v1"), b"v1")
    CapabilityImportService(root).promote(
        staging, target_agent_did=_AGENT, operator_did=_OPERATOR, signer=signer, config_path=config
    )
    resolver = _resolver(tmp_path, config, signer)
    folder = root / "skills" / "reporter"
    assert resolver.resolve(folder, "agent-skills") is None
    assert resolver.revision_history(folder) == []


def test_unenrolled_skill_with_revision_evidence_is_a_reset(tmp_path: Path) -> None:
    root, config, signer = _setup(tmp_path)
    staging = _import(tmp_path, CapabilityImportService(root), root, "v1", _body("v1"), b"v1")
    CapabilityImportService(root).promote(
        staging, target_agent_did=_AGENT, operator_did=_OPERATOR, signer=signer, config_path=config
    )
    folder = root / "skills" / "reporter"
    resolver = _resolver(tmp_path, config, signer)
    resolver.revise(
        folder,
        _body("v1"),
        expected_sha256=hashlib.sha256(_body("v1")).hexdigest(),
        signer=signer,
        operator_did=_OPERATOR,
    )
    # The anchor journal disappears but the signed revision evidence remains.
    for path in (tmp_path / "anchors").iterdir():
        path.unlink()
    fresh = _resolver(tmp_path, config, signer)
    with pytest.raises(ValueError, match="unavailable"):
        fresh.resolve(folder, "agent-skills")
    with pytest.raises(ValueError, match="unavailable"):
        fresh.revision_history(folder)


def test_federal_still_requires_explicit_enrollment(tmp_path: Path) -> None:
    root, config, signer = _setup(tmp_path, tier="federal")
    staging = _import(tmp_path, CapabilityImportService(root), root, "v1", _body("v1"), b"v1")
    CapabilityImportService(root).promote(
        staging, target_agent_did=_AGENT, operator_did=_OPERATOR, signer=signer, config_path=config
    )
    with pytest.raises(ValueError, match="unavailable"):
        _resolver(tmp_path, config, signer).resolve(root / "skills" / "reporter", "agent-skills")


def test_second_import_without_an_authority_still_refuses(tmp_path: Path) -> None:
    root, config, signer = _setup(tmp_path)
    service = CapabilityImportService(root)
    first = _import(tmp_path, service, root, "v1", _body("v1"), b"v1")
    service.promote(
        first, target_agent_did=_AGENT, operator_did=_OPERATOR, signer=signer, config_path=config
    )
    second = _import(tmp_path, service, root, "v2", _body("v2"), b"v2")
    with pytest.raises(ValueError, match="already exists"):
        service.promote(
            second,
            target_agent_did=_AGENT,
            operator_did=_OPERATOR,
            signer=signer,
            config_path=config,
        )


def test_v1_then_v2_import_creates_two_revisions_and_rollback_restores_v1(
    tmp_path: Path,
) -> None:
    root, config, signer = _setup(tmp_path)
    service = CapabilityImportService(root)
    resolver = _resolver(tmp_path, config, signer)
    folder = root / "skills" / "reporter"
    first = _import(tmp_path, service, root, "v1", _body("v1"), b"schema v1")
    service.promote(
        first,
        target_agent_did=_AGENT,
        operator_did=_OPERATOR,
        signer=signer,
        config_path=config,
        revisions=resolver,
    )
    second = _import(tmp_path, service, root, "v2", _body("v2"), b"schema v2")
    promoted = service.promote(
        second,
        target_agent_did=_AGENT,
        operator_did=_OPERATOR,
        signer=signer,
        config_path=config,
        revisions=resolver,
    )
    assert folder / "SKILL.md" in promoted
    history = resolver.revision_history(folder)
    assert [(version, body) for _, version, body, _ in history] == [
        (2, _body("v2").decode()),
        (1, _body("v1").decode()),
    ]
    active = resolver.resolve(folder, "agent-skills")
    assert active is not None
    assert (active.parent / "references" / "schema.txt").read_bytes() == b"schema v2"
    v1_digest = history[1][0]
    resolver.activate_prior(
        folder,
        v1_digest,
        expected_sha256=hashlib.sha256(_body("v2")).hexdigest(),
        signer=signer,
        operator_did=_OPERATOR,
    )
    restored = resolver.resolve(folder, "agent-skills")
    assert restored is not None
    assert resolver.read_current(folder, restored) == _body("v1").decode()
    assert (restored.parent / "references" / "schema.txt").read_bytes() == b"schema v1"
    assert [version for _, version, _, _ in resolver.revision_history(folder)] == [3, 2, 1]
    # A third import extends the same lineage.
    third = _import(tmp_path, service, root, "v3", _body("v3"), b"schema v3")
    service.promote(
        third,
        target_agent_did=_AGENT,
        operator_did=_OPERATOR,
        signer=signer,
        config_path=config,
        revisions=resolver,
    )
    assert resolver.revision_history(folder)[0][1] == 4


def test_failed_update_leaves_the_enrolled_original_active(tmp_path: Path) -> None:
    root, config, signer = _setup(tmp_path)
    service = CapabilityImportService(root)
    resolver = _resolver(tmp_path, config, signer)
    first = _import(tmp_path, service, root, "v1", _body("v1"), b"v1")
    service.promote(
        first,
        target_agent_did=_AGENT,
        operator_did=_OPERATOR,
        signer=signer,
        config_path=config,
        revisions=resolver,
    )
    # An update may only carry resources an anchored revision can hold.
    second = _import(
        tmp_path,
        service,
        root,
        "v2",
        _body("v2"),
        b"v2",
        extra={"scripts/extract.py": b"print('x')\n"},
    )
    with pytest.raises(ValueError, match="unsafe file"):
        service.promote(
            second,
            target_agent_did=_AGENT,
            operator_did=_OPERATOR,
            signer=signer,
            config_path=config,
            revisions=resolver,
        )
    folder = root / "skills" / "reporter"
    assert (folder / "SKILL.md").read_bytes() == _body("v1")
    assert resolver.revision_history(folder) == []
