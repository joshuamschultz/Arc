"""W0-skill revision chain: scripts in revisions, verified per-file reads, first-edit
enrollment, and the operator-signed improver writer.

Every skill byte the agent reads or runs must trace back to an operator signature:
either the installed original's operator sidecars + approvals, or the active
revision's operator-signed manifest. These tests pin the public seam the script
runner and ``read_skill_file`` use, and that an update never drops the original.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from arctrust import ArtifactSignature, FileJournalAnchor, InProcessSigner

from arcagent.modules.capability_import.archive import intake
from arcagent.modules.capability_import.models import CapabilityImportLimits
from arcagent.modules.capability_import.revisions import (
    AnchoredSkillRevisionResolver,
    OperatorSkillRevisionWriter,
    skill_revision_scope,
)
from arcagent.modules.capability_import.service import CapabilityImportService

_AGENT = "did:arc:agent:reporter"
_OPERATOR = "did:arc:operator:test"
_SCRIPT_V1 = b"print('v1')\n"
_SCRIPT_V2 = b"print('v2')\n"


def _body(step: str) -> bytes:
    return (
        "---\nname: reporter\ndescription: Create reports\n---\n"
        "## Resources\nnone\n## Contract\nfollow the steps\n"
        "## Knowledge\nsource data\n## Steps\n"
        f"{step}\n"
        "## Anti Patterns\nnone\n## Examples\nexample\n## Validation\ncheck\n"
    ).encode()


def _setup(tmp_path: Path) -> tuple[Path, Path, InProcessSigner, AnchoredSkillRevisionResolver]:
    root = tmp_path / "agent" / "capabilities"
    root.mkdir(parents=True)
    config = tmp_path / "agent" / "arcagent.toml"
    config.write_text('[security]\ntier = "personal"\n')
    signer = InProcessSigner(bytes(range(32)))
    anchors = tmp_path / "anchors"
    resolver = AnchoredSkillRevisionResolver(
        agent_did=_AGENT,
        config_path=config,
        anchor_factory=lambda did, name: FileJournalAnchor(
            anchors, scope=skill_revision_scope(did, name), signer=signer
        ),
    )
    return root, config, signer, resolver


def _promote(
    tmp_path: Path,
    root: Path,
    config: Path,
    signer: InProcessSigner,
    label: str,
    files: dict[str, bytes],
    resolver: AnchoredSkillRevisionResolver | None = None,
) -> None:
    service = CapabilityImportService(root)
    source = tmp_path / f"source-{label}"
    folder = source / "skills" / "reporter"
    for relative, content in files.items():
        (folder / relative).parent.mkdir(parents=True, exist_ok=True)
        (folder / relative).write_bytes(content)
    imported = intake(source, root)
    service.review(imported, target_agent_did=_AGENT, limits=CapabilityImportLimits())
    service.promote(
        imported.staging_dir,
        target_agent_did=_AGENT,
        operator_did=_OPERATOR,
        signer=signer,
        config_path=config,
        revisions=resolver,
    )


def _v1(tmp_path: Path) -> tuple[Path, Path, InProcessSigner, AnchoredSkillRevisionResolver]:
    root, config, signer, resolver = _setup(tmp_path)
    _promote(
        tmp_path,
        root,
        config,
        signer,
        "v1",
        {"SKILL.md": _body("v1"), "scripts/extract.py": _SCRIPT_V1, "references/a.md": b"a1"},
    )
    return root, config, signer, resolver


def test_update_import_carrying_scripts_becomes_a_signed_revision(tmp_path: Path) -> None:
    root, config, signer, resolver = _v1(tmp_path)
    _promote(
        tmp_path,
        root,
        config,
        signer,
        "v2",
        {"SKILL.md": _body("v2"), "scripts/extract.py": _SCRIPT_V2, "references/a.md": b"a2"},
        resolver,
    )
    folder = root / "skills" / "reporter"
    history = resolver.revision_history(folder)
    assert [version for _, version, _, _ in history] == [2, 1]
    active = resolver.active_folder(folder)
    assert active is not None
    script = active / "scripts" / "extract.py"
    assert script.read_bytes() == _SCRIPT_V2
    sidecar = ArtifactSignature.from_json(
        (active / "scripts" / "extract.py.arcsig").read_text(encoding="utf-8")
    )
    assert sidecar is not None
    assert sidecar.signer_did == _OPERATOR
    listed = {row["path"] for row in json.loads((active / "manifest.json").read_bytes())["files"]}
    assert {"scripts/extract.py", "scripts/extract.py.arcsig"} <= listed


def test_read_verified_file_serves_the_unenrolled_signed_original(tmp_path: Path) -> None:
    root, _config, _signer, resolver = _v1(tmp_path)
    folder = root / "skills" / "reporter"
    assert resolver.active_folder(folder) is None
    assert resolver.read_verified_file(folder, "scripts/extract.py") == _SCRIPT_V1
    assert resolver.read_verified_file(folder, "references/a.md") == b"a1"


def test_read_verified_file_serves_the_active_revision(tmp_path: Path) -> None:
    root, config, signer, resolver = _v1(tmp_path)
    _promote(
        tmp_path,
        root,
        config,
        signer,
        "v2",
        {"SKILL.md": _body("v2"), "scripts/extract.py": _SCRIPT_V2},
        resolver,
    )
    folder = root / "skills" / "reporter"
    assert resolver.read_verified_file(folder, "scripts/extract.py") == _SCRIPT_V2
    with pytest.raises(ValueError):
        resolver.read_verified_file(folder, "references/a.md")  # not in v2


@pytest.mark.parametrize(
    "relpath",
    ["../reporter/SKILL.md", "/etc/passwd", "scripts/../../x", "SKILL.md.arcsig", "manifest.json"],
)
def test_read_verified_file_refuses_unsafe_paths(tmp_path: Path, relpath: str) -> None:
    root, _config, _signer, resolver = _v1(tmp_path)
    with pytest.raises(ValueError):
        resolver.read_verified_file(root / "skills" / "reporter", relpath)


def test_tampered_original_file_is_refused(tmp_path: Path) -> None:
    root, _config, _signer, resolver = _v1(tmp_path)
    folder = root / "skills" / "reporter"
    (folder / "scripts" / "extract.py").write_bytes(b"import os; os.system('id')\n")
    with pytest.raises(ValueError):
        resolver.read_verified_file(folder, "scripts/extract.py")


def test_tampered_revision_file_is_refused(tmp_path: Path) -> None:
    root, config, signer, resolver = _v1(tmp_path)
    _promote(
        tmp_path,
        root,
        config,
        signer,
        "v2",
        {"SKILL.md": _body("v2"), "scripts/extract.py": _SCRIPT_V2},
        resolver,
    )
    folder = root / "skills" / "reporter"
    active = resolver.active_folder(folder)
    assert active is not None
    (active / "scripts" / "extract.py").write_bytes(b"evil\n")
    with pytest.raises(ValueError):
        resolver.read_verified_file(folder, "scripts/extract.py")


def test_symlinked_file_in_original_is_refused(tmp_path: Path) -> None:
    root, _config, _signer, resolver = _v1(tmp_path)
    folder = root / "skills" / "reporter"
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    (folder / "references" / "link.md").symlink_to(secret)
    with pytest.raises(ValueError):
        resolver.read_verified_file(folder, "references/link.md")


def test_first_edit_of_an_unenrolled_skill_enrolls_the_original_first(tmp_path: Path) -> None:
    root, _config, signer, resolver = _v1(tmp_path)
    folder = root / "skills" / "reporter"
    resolver.revise(
        folder,
        _body("edited"),
        expected_sha256=hashlib.sha256(_body("v1")).hexdigest(),
        signer=signer,
        operator_did=_OPERATOR,
    )
    history = resolver.revision_history(folder)
    assert [(version, body) for _, version, body, _ in history] == [
        (2, _body("edited").decode()),
        (1, _body("v1").decode()),
    ]
    # The original's script survives into both revisions.
    assert resolver.read_verified_file(folder, "scripts/extract.py") == _SCRIPT_V1


def test_reserved_revision_manifest_name_is_refused_before_enrollment(tmp_path: Path) -> None:
    root, config, signer, resolver = _v1(tmp_path)
    with pytest.raises(ValueError, match="manifest.json"):
        _promote(
            tmp_path,
            root,
            config,
            signer,
            "v2",
            {"SKILL.md": _body("v2"), "manifest.json": b"{}"},
            resolver,
        )
    assert resolver.revision_history(root / "skills" / "reporter") == []


def test_operator_writer_commits_an_overlay_as_a_new_revision(tmp_path: Path) -> None:
    root, _config, signer, resolver = _v1(tmp_path)
    folder = root / "skills" / "reporter"
    writer = OperatorSkillRevisionWriter(
        authority=lambda: resolver,
        signer=signer,
        operator_did=_OPERATOR,
        folder_of=lambda name: folder if name == "reporter" else None,
    )
    digest = writer.commit("reporter", {"SKILL.md": _body("improved")}, reason="improver")
    history = resolver.revision_history(folder)
    assert history[0][0] == digest
    assert [(version, body) for _, version, body, _ in history] == [
        (2, _body("improved").decode()),
        (1, _body("v1").decode()),
    ]
    active = resolver.active_folder(folder)
    assert active is not None
    # Unchanged files carry forward; every sidecar is the operator's.
    assert (active / "scripts" / "extract.py").read_bytes() == _SCRIPT_V1
    for sidecar in active.rglob("*.arcsig"):
        signature = ArtifactSignature.from_json(sidecar.read_text(encoding="utf-8"))
        assert signature is not None
        assert signature.signer_did == _OPERATOR
        assert signature.signer_did != _AGENT
    with pytest.raises(ValueError):
        writer.commit("missing", {"SKILL.md": _body("x")}, reason="improver")
    with pytest.raises(ValueError):
        writer.commit("reporter", {"../escape.md": b"x"}, reason="improver")
