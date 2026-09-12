"""SPEC-081 RED wave — reject auto-executing and opaque artifacts (REQ-400).

T-1053: each unsafe/opaque artifact must be rejected with a typed error that
names the specific offending path:

  - ``__init__.py``                         (auto-import package marker)
  - ``.pyc`` / ``.pyo`` bytecode
  - a nested archive (``.zip`` inside)
  - a symlink / special entry
  - a compiled binary by extension (``.so``/``.dylib``/``.dll``/``.exe``)
  - a compiled binary by MAGIC BYTES under an innocent extension
    (ELF ``\\x7fELF``, Mach-O ``\\xcf\\xfa\\xed\\xfe``, PE ``MZ``)

RED today:
  - ``.so`` and magic-byte binaries slip through (no extension/content ban).
  - the already-rejected cases raise a GENERIC message that does not name the
    offending path, so REQ-400's "report the specific offending path" fails.

Abuse cases here should be mirrored into ``scripts/run_adversarial_tests.py``
when T-1054 lands.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from arcagent.modules.capability_import.archive import intake
from arcagent.modules.capability_import.errors import CapabilityImportError

_ELF = b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 24
_MACHO = b"\xcf\xfa\xed\xfe" + b"\x07\x00\x00\x01" + b"\x00" * 24
_PE = b"MZ\x90\x00" + b"\x00" * 28


def _skill_md() -> bytes:
    return (
        b"---\nname: imported\ndescription: imported skill\n---\n"
        b"## Files\nnone\n## Contract\nnone\n## Knowledge\nnone\n"
        b"## Steps\nUse it.\n## Red Flags & Rationalizations\nnone\n"
        b"## Validation\nnone\n## Examples\nnone\n"
    )


def _archive_with_file(path: Path, offending: str, content: bytes) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("skills/imported/SKILL.md", _skill_md())
        archive.writestr(offending, content)
    return path


def _archive_with_symlink(path: Path, offending: str, points_to: bytes) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("skills/imported/SKILL.md", _skill_md())
        info = zipfile.ZipInfo(offending)
        info.external_attr = 0o120777 << 16  # S_IFLNK | 0777
        archive.writestr(info, points_to)
    return path


@pytest.mark.parametrize(
    ("offending", "content"),
    [
        ("skills/imported/scripts/__init__.py", b"# package marker\n"),
        ("skills/imported/scripts/mod.pyc", b"\x00compiled\x00"),
        ("skills/imported/scripts/mod.pyo", b"\x00compiled\x00"),
        ("skills/imported/references/inner.zip", b"PK\x03\x04nested"),
        ("skills/imported/references/lib.so", b"\x00binary\x00"),
        ("skills/imported/references/plugin.dylib", b"\x00binary\x00"),
        ("skills/imported/references/plugin.dll", b"\x00binary\x00"),
        ("skills/imported/references/tool.exe", b"\x00binary\x00"),
        ("skills/imported/references/elf-note.md", _ELF),
        ("skills/imported/references/macho-note.txt", _MACHO),
        ("skills/imported/references/pe-note.md", _PE),
    ],
)
def test_opaque_artifact_is_rejected_naming_the_offending_path(
    tmp_path: Path, offending: str, content: bytes
) -> None:
    archive = _archive_with_file(tmp_path / "bad.zip", offending, content)
    target = tmp_path / "agent" / "capabilities"

    with pytest.raises(CapabilityImportError) as exc:
        intake(archive, target)

    assert Path(offending).name in str(exc.value), (
        f"error must name the offending path {offending!r}, got: {exc.value!r}"
    )
    assert not (target / "imports" / ".staging").exists()


def test_symlink_entry_is_rejected_naming_the_offending_path(tmp_path: Path) -> None:
    offending = "skills/imported/references/link.md"
    archive = _archive_with_symlink(tmp_path / "symlink.zip", offending, b"/etc/passwd")
    target = tmp_path / "agent" / "capabilities"

    with pytest.raises(CapabilityImportError) as exc:
        intake(archive, target)

    assert Path(offending).name in str(exc.value), (
        f"error must name the offending symlink {offending!r}, got: {exc.value!r}"
    )
    assert not (target / "imports" / ".staging").exists()
