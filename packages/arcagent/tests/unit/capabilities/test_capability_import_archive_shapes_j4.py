"""J4 M3 / G9 — intake accepts the pack shapes a person actually zips.

A flat ZIP (``SKILL.md`` at the root), a skill folder beside a ``tools/`` root,
and the same shapes wrapped in one folder or supplied as a local directory.
Anything that is not a skill folder keeps being refused.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from arcagent.modules.capability_import.archive import intake
from arcagent.modules.capability_import.errors import CapabilityImportLayoutError


def _zip(path: Path, entries: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return path


def _skill() -> bytes:
    return b"---\nname: imported\ndescription: imported skill\n---\n## Steps\nUse it.\n"


def _paths(source: Path, tmp_path: Path) -> list[str]:
    return [entry.path for entry in intake(source, tmp_path / "agent" / "capabilities").files]


def test_intake_accepts_a_flat_root_skill_zip(tmp_path: Path) -> None:
    """``SKILL.md`` at the ZIP root: the root IS the skill, named by its frontmatter."""
    archive = _zip(
        tmp_path / "flat.zip",
        {
            "SKILL.md": _skill(),
            "references/advanced.md": b"advanced",
            "scripts/extract.py": b"print('x')\n",
            "LICENSE.txt": b"MIT",
        },
    )

    assert _paths(archive, tmp_path) == [
        "skills/imported/LICENSE.txt",
        "skills/imported/SKILL.md",
        "skills/imported/references/advanced.md",
        "skills/imported/scripts/extract.py",
    ]


def test_intake_accepts_a_flat_root_skill_directory(tmp_path: Path) -> None:
    """The same flat shape from a local folder (``arc capability-import import <dir>``)."""
    source = tmp_path / "pdf"
    (source / "references").mkdir(parents=True)
    (source / "SKILL.md").write_bytes(_skill())
    (source / "references" / "guide.md").write_bytes(b"guide")

    assert _paths(source, tmp_path) == [
        "skills/imported/SKILL.md",
        "skills/imported/references/guide.md",
    ]


def test_intake_keeps_supplier_metadata_at_root_of_a_flat_skill(tmp_path: Path) -> None:
    archive = _zip(
        tmp_path / "flat.zip",
        {"SKILL.md": _skill(), "capability-import.toml": b'vendor = "acme"\n'},
    )

    assert _paths(archive, tmp_path) == ["capability-import.toml", "skills/imported/SKILL.md"]


def test_intake_refuses_a_flat_skill_without_a_safe_frontmatter_name(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "flat.zip", {"SKILL.md": b"---\ndescription: no name\n---\nb\n"})

    with pytest.raises(CapabilityImportLayoutError, match="frontmatter name"):
        intake(archive, tmp_path / "agent" / "capabilities")


def test_intake_refuses_a_flat_skill_whose_name_escapes(tmp_path: Path) -> None:
    archive = _zip(
        tmp_path / "flat.zip",
        {"SKILL.md": b"---\nname: ../evil\ndescription: x\n---\nb\n"},
    )

    with pytest.raises(CapabilityImportLayoutError, match="frontmatter name"):
        intake(archive, tmp_path / "agent" / "capabilities")


def test_intake_accepts_a_skill_folder_beside_tools(tmp_path: Path) -> None:
    """A skill folder with a ``tools/`` root beside it (J4 tool_pack.zip)."""
    archive = _zip(
        tmp_path / "mixed.zip",
        {
            "pdf-tools/SKILL.md": _skill(),
            "pdf-tools/references/r.md": b"r",
            "tools/word_count.py": b"from arcagent import tool\n",
        },
    )

    assert _paths(archive, tmp_path) == [
        "skills/pdf-tools/SKILL.md",
        "skills/pdf-tools/references/r.md",
        "tools/word_count.py",
    ]


def test_intake_accepts_a_wrapped_mixed_root(tmp_path: Path) -> None:
    archive = _zip(
        tmp_path / "wrapped.zip",
        {
            "pack/pdf-tools/SKILL.md": _skill(),
            "pack/tools/word_count.py": b"from arcagent import tool\n",
        },
    )

    assert _paths(archive, tmp_path) == ["skills/pdf-tools/SKILL.md", "tools/word_count.py"]


def test_intake_still_refuses_an_unrelated_root_beside_tools(tmp_path: Path) -> None:
    """Only a folder that IS a skill (holds SKILL.md) is reparented."""
    archive = _zip(
        tmp_path / "mixed.zip",
        {"notes/readme.md": b"x", "tools/word_count.py": b"from arcagent import tool\n"},
    )

    with pytest.raises(CapabilityImportLayoutError, match="unsupported path"):
        intake(archive, tmp_path / "agent" / "capabilities")
