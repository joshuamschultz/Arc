"""Tests for non-executing capability-import archive intake."""

from __future__ import annotations

import stat
import zipfile
from pathlib import Path

import pytest

from arcagent.modules.capability_import.archive import intake
from arcagent.modules.capability_import.models import CapabilityImportLimits


def _zip(path: Path, entries: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return path


def _skill() -> bytes:
    return b"---\nname: imported\ndescription: imported skill\n---\n## Steps\nUse it.\n"


def test_intake_extracts_only_supported_layout_to_content_addressed_roots(tmp_path: Path) -> None:
    archive = _zip(
        tmp_path / "capabilities.zip",
        {
            "tools/hello.py": b"from arcagent import tool\n",
            "skills/imported/SKILL.md": _skill(),
            "skills/imported/references/guide.md": b"guide",
        },
    )

    result = intake(archive, tmp_path / "agent" / "capabilities")

    assert result.archive_sha256
    assert result.staging_dir == (
        tmp_path / "agent" / "capabilities" / "imports" / ".staging" / result.import_id
    )
    assert [entry.path for entry in result.files] == [
        "skills/imported/SKILL.md",
        "skills/imported/references/guide.md",
        "tools/hello.py",
    ]
    assert (
        result.staging_dir / "tools" / "hello.py"
    ).read_bytes() == b"from arcagent import tool\n"
    assert stat.S_IMODE(result.staging_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((result.staging_dir / "tools" / "hello.py").stat().st_mode) == 0o600
    assert stat.S_IMODE(result.quarantine_path.stat().st_mode) == 0o600


def test_intake_accepts_normal_folder_zip_with_directories_and_one_wrapper(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "portable.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for directory in (
            "portable/",
            "portable/skills/",
            "portable/skills/imported/",
            "portable/skills/imported/references/",
            "portable/tools/",
        ):
            archive.writestr(directory, b"")
        archive.writestr("portable/skills/imported/SKILL.md", _skill())
        archive.writestr("portable/skills/imported/references/guide.md", b"guide")
        archive.writestr("portable/tools/hello.py", b"x")

    result = intake(archive_path, tmp_path / "agent" / "capabilities")

    assert [entry.path for entry in result.files] == [
        "skills/imported/SKILL.md",
        "skills/imported/references/guide.md",
        "tools/hello.py",
    ]


def test_intake_is_deterministic_for_same_archive(tmp_path: Path) -> None:
    archive = _zip(
        tmp_path / "capabilities.zip",
        {"tools/hello.py": b"x", "skills/imported/SKILL.md": _skill()},
    )
    root = tmp_path / "agent" / "capabilities"

    first = intake(archive, root)
    second = intake(archive, root)

    assert first.import_id == second.import_id
    assert first.files == second.files
    assert first.staging_dir == second.staging_dir


def test_intake_accepts_local_tree_without_following_links(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "tools").mkdir(parents=True)
    (source / "skills" / "imported").mkdir(parents=True)
    (source / "tools" / "hello.py").write_bytes(b"x")
    (source / "skills" / "imported" / "SKILL.md").write_bytes(_skill())

    result = intake(source, tmp_path / "agent" / "capabilities")

    assert [entry.path for entry in result.files] == ["skills/imported/SKILL.md", "tools/hello.py"]


@pytest.mark.parametrize(
    "limits",
    [
        CapabilityImportLimits(max_files=1),
        CapabilityImportLimits(max_depth=2),
        CapabilityImportLimits(max_compressed_bytes=2),
        CapabilityImportLimits(max_expanded_bytes=2),
        CapabilityImportLimits(max_file_bytes=2),
        CapabilityImportLimits(max_compression_ratio=1),
    ],
)
def test_limits_are_enforced_before_staging(
    tmp_path: Path, limits: CapabilityImportLimits
) -> None:
    archive = _zip(
        tmp_path / "capabilities.zip",
        {"tools/hello.py": b"abcdef", "skills/imported/SKILL.md": _skill()},
    )
    target = tmp_path / "agent" / "capabilities"

    with pytest.raises(Exception):
        intake(archive, target, limits=limits)

    assert not (target / "imports" / ".staging").exists()
