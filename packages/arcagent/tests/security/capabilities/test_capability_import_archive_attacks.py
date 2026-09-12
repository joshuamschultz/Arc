"""Hostile archive and local-tree cases for capability-import intake."""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

from arcagent.modules.capability_import.archive import intake
from arcagent.modules.capability_import.errors import CapabilityImportError


def _skill() -> bytes:
    return b"---\nname: imported\ndescription: imported skill\n---\n## Steps\nUse it.\n"


def _archive(path: Path, names: list[str]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name in names:
            archive.writestr(name, _skill() if name.endswith("SKILL.md") else b"x")
    return path


@pytest.mark.parametrize(
    "name",
    [
        "../tools/hello.py",
        "/tools/hello.py",
        "C:/tools/hello.py",
        "\\\\server\\share\\tools\\hello.py",
        "tools/../hello.py",
        "tools/hello\x00.py",
        "tools\\hello.py",
    ],
)
def test_zip_path_attacks_are_rejected_without_staging(tmp_path: Path, name: str) -> None:
    archive = _archive(tmp_path / "bad.zip", [name, "skills/imported/SKILL.md"])
    target = tmp_path / "agent" / "capabilities"

    with pytest.raises(CapabilityImportError):
        intake(archive, target)

    assert not (target / "imports" / ".staging").exists()


def test_zip_duplicate_and_unicode_case_collisions_are_rejected(tmp_path: Path) -> None:
    duplicate = _archive(
        tmp_path / "duplicate.zip",
        ["tools/hello.py", "tools/hello.py", "skills/imported/SKILL.md"],
    )
    collision = _archive(
        tmp_path / "collision.zip",
        ["tools/Hello.py", "tools/hello.py", "skills/imported/SKILL.md"],
    )

    with pytest.raises(CapabilityImportError):
        intake(duplicate, tmp_path / "one" / "capabilities")
    with pytest.raises(CapabilityImportError):
        intake(collision, tmp_path / "two" / "capabilities")


def test_zip_symlink_and_encrypted_members_are_rejected(tmp_path: Path) -> None:
    symlink = tmp_path / "symlink.zip"
    with zipfile.ZipFile(symlink, "w") as archive:
        info = zipfile.ZipInfo("tools/hello.py")
        info.external_attr = 0o120777 << 16
        archive.writestr(info, b"/etc/passwd")
        archive.writestr("skills/imported/SKILL.md", _skill())
    encrypted = tmp_path / "encrypted.zip"
    with zipfile.ZipFile(encrypted, "w") as archive:
        archive.writestr("tools/hello.py", b"x")
        archive.writestr("skills/imported/SKILL.md", _skill())
    raw = bytearray(encrypted.read_bytes())
    for marker, offset in ((bytes((80, 75, 3, 4)), 6), (bytes((80, 75, 1, 2)), 8)):
        start = 0
        while (index := raw.find(marker, start)) >= 0:
            raw[index + offset] |= 0x01
            start = index + len(marker)
    encrypted.write_bytes(raw)

    with pytest.raises(CapabilityImportError):
        intake(symlink, tmp_path / "one" / "capabilities")
    with pytest.raises(CapabilityImportError):
        intake(encrypted, tmp_path / "two" / "capabilities")


@pytest.mark.parametrize(
    "name",
    ["tools/__init__.py", "tools/hello.pyc", "tools/inner.zip", "other/file.txt"],
)
def test_unsupported_payloads_are_rejected(tmp_path: Path, name: str) -> None:
    archive = _archive(tmp_path / "bad.zip", [name, "skills/imported/SKILL.md"])
    with pytest.raises(CapabilityImportError):
        intake(archive, tmp_path / "agent" / "capabilities")


def test_local_links_hardlinks_and_special_files_are_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "tools").mkdir(parents=True)
    (source / "skills" / "imported").mkdir(parents=True)
    tool = source / "tools" / "hello.py"
    tool.write_bytes(b"x")
    (source / "skills" / "imported" / "SKILL.md").write_bytes(_skill())
    link = source / "tools" / "linked.py"
    link.symlink_to(tool)
    with pytest.raises(CapabilityImportError):
        intake(source, tmp_path / "one" / "capabilities")
    link.unlink()
    os.link(tool, source / "tools" / "hard.py")
    with pytest.raises(CapabilityImportError):
        intake(source, tmp_path / "two" / "capabilities")


@pytest.mark.parametrize(
    "name",
    [
        "__MACOSX/../../tools/escape.py",
        "__MACOSX/../evil.py",
    ],
)
def test_junk_prefixes_cannot_smuggle_traversal(tmp_path: Path, name: str) -> None:
    """Dropping ``__MACOSX`` noise must not bypass path-traversal rejection."""
    archive = _archive(tmp_path / "smuggle.zip", [name, "skills/imported/SKILL.md"])
    target = tmp_path / "agent" / "capabilities"

    with pytest.raises(CapabilityImportError):
        intake(archive, target)

    assert not (target / "imports" / ".staging").exists()


def test_bare_folder_with_unsafe_name_is_not_reparented(tmp_path: Path) -> None:
    """A single skill folder whose name is unsafe is rejected, never re-parented."""
    archive = _archive(tmp_path / "unsafe.zip", ["bad name/SKILL.md"])

    with pytest.raises(CapabilityImportError):
        intake(archive, tmp_path / "agent" / "capabilities")


def test_dropping_all_content_as_junk_still_rejects_empty_import(tmp_path: Path) -> None:
    """An archive of nothing but metadata has no capability and is rejected."""
    archive = _archive(
        tmp_path / "onlyjunk.zip",
        ["__MACOSX/skills/imported/._SKILL.md", ".DS_Store"],
    )

    with pytest.raises(CapabilityImportError):
        intake(archive, tmp_path / "agent" / "capabilities")
