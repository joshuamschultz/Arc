"""Sign a whole skill folder as one operator pack approval (J4 G8, PLAN item 42).

Re-signing only ``SKILL.md`` leaves every reference, script and asset the model
reads or runs under whatever signature it had before — or none. An operator who
re-signs a skill is approving the skill, so the approval covers every data file
in its folder: each gets an operator ``.arcsig`` sidecar and a TOFU pin under the
same name capability import's promote uses (:func:`approval_name`), so the
runtime verifier and the import path agree on what was approved.

The pack is checked BEFORE anything is written: a symlink, a hard link or a
special file anywhere in the folder refuses the whole approval (a link could
make the operator sign bytes outside the pack). A failure part-way restores the
prior sidecars and the agent's validator config, so a half-signed pack never
looks trusted. One ``capability.signed`` audit record is emitted per pack.
"""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime
from pathlib import Path

from arctrust import (
    AuditEvent,
    AuditSink,
    Signer,
    approve,
    disapprove,
    emit,
    hash_source,
    load_validators,
    persist_validators,
    pin_key,
    unpin_key,
)

from arcagent.capabilities.artifact_signing import (
    SIDECAR_SUFFIX,
    key_still_in_use,
    load_signature,
    sidecar_path,
    write_signature_with_signer,
)
from arcagent.capabilities.capability_loader import pin_name_for_path
from arcagent.modules.capability_import.service import approval_name, approval_source

_MAX_PACK_FILES = 512
_MAX_DEPTH = 12


class SkillPackError(ValueError):
    """The skill folder cannot be approved as a pack (unsafe or incomplete)."""


def skill_pack_files(folder: Path) -> list[Path]:
    """Every data file in ``folder``, sorted; refuse links and special files.

    ``.arcsig`` sidecars are signatures, not pack content, so they are skipped.
    """
    if folder.is_symlink() or not folder.is_dir():
        raise SkillPackError("skill folder is not a regular directory")
    files: list[Path] = []
    _collect(folder, files, depth=1)
    if (folder / "SKILL.md") not in files:
        raise SkillPackError("skill folder has no SKILL.md")
    return sorted(files)


def _collect(directory: Path, files: list[Path], *, depth: int) -> None:
    if depth > _MAX_DEPTH:
        raise SkillPackError("skill folder is nested too deeply")
    for entry in sorted(os.scandir(directory), key=lambda item: item.name):
        path = Path(entry.path)
        mode = entry.stat(follow_symlinks=False)
        if stat.S_ISLNK(mode.st_mode):
            raise SkillPackError(f"skill folder contains a symlink: {path.name}")
        if stat.S_ISDIR(mode.st_mode):
            _collect(path, files, depth=depth + 1)
            continue
        if not stat.S_ISREG(mode.st_mode) or mode.st_nlink != 1:
            raise SkillPackError(f"skill folder contains a linked or special file: {path.name}")
        if path.name.endswith(SIDECAR_SUFFIX):
            continue
        files.append(path)
        if len(files) > _MAX_PACK_FILES:
            raise SkillPackError("skill folder has too many files")


def sign_skill_folder(
    skill_md: Path,
    *,
    signer_did: str,
    signer: Signer,
    config_path: Path,
    audit_sink: AuditSink | None = None,
) -> tuple[Path, ...]:
    """Sign every data file of the skill that owns ``skill_md``; return them.

    Raises:
        SkillPackError: the folder holds a link/special file or lacks SKILL.md.
        ValueError: ``SKILL.md`` is not UTF-8 text.
        OSError: a file or the config could not be read/written.
    """
    folder = skill_md.parent
    files = skill_pack_files(folder)
    contents = {path: path.read_bytes() for path in files}
    skill_source = contents[folder / "SKILL.md"].decode("utf-8")
    sidecars_before = {path: _read_optional(sidecar_path(path)) for path in files}
    validators_before = load_validators(config_path)
    try:
        for path, content in contents.items():
            write_signature_with_signer(path, content, signer_did=signer_did, signer=signer)
        pin_key(config_path, public_key=signer.public_key)
        timestamp = datetime.now(UTC).isoformat()
        for path, content in contents.items():
            approve(
                config_path,
                name=_pin_name(folder, path.relative_to(folder).as_posix(), path),
                source=approval_source(content),
                approver=signer_did,
                timestamp=timestamp,
            )
    except Exception:
        persist_validators(config_path, validators_before)
        for path, previous in sidecars_before.items():
            _restore_optional(sidecar_path(path), previous)
        raise
    if audit_sink is not None:
        emit(
            AuditEvent(
                actor_did=signer_did,
                action="capability.signed",
                target=str(skill_md),
                outcome="signed",
                payload_hash=hash_source(skill_source),
                extra={"pack_files": len(files), "scope": "skill_folder"},
            ),
            audit_sink,
        )
    return tuple(files)


def revoke_skill_folder(
    skill_md: Path,
    *,
    config_path: Path,
    operator_did: str,
    audit_sink: AuditSink | None = None,
) -> int:
    """Revoke the whole skill pack that owns ``skill_md``; return the file count.

    The inverse of :func:`sign_skill_folder`. Approval signs and pins every data
    file, so revocation covers every file that has a sidecar OR a pin, including
    a sidecar whose file has since been deleted: leaving it would re-enable the
    file the moment something recreated it. Links are never followed.
    Idempotent; the trusted key is unpinned once, and only when no other artifact
    under the agent root is still signed by it.
    """
    folder = skill_md.parent
    names = _pack_member_names(folder)
    keys: set[str] = set()
    for relative in names:
        member = folder / relative
        manifest = load_signature(member)
        if manifest is not None:
            keys.add(manifest.public_key)
        sidecar_path(member).unlink(missing_ok=True)
        disapprove(config_path, name=_pin_name(folder, relative, member))
    for public_key in keys:
        if not key_still_in_use(config_path.parent, public_key):
            unpin_key(config_path, public_key=bytes.fromhex(public_key))
    if audit_sink is not None:
        emit(
            AuditEvent(
                actor_did=operator_did,
                action="capability.signature_revoked",
                target=str(skill_md),
                outcome="revoked",
                payload_hash=_skill_source_hash(skill_md),
                extra={"pack_files": len(names), "scope": "skill_folder"},
            ),
            audit_sink,
        )
    return len(names)


def _skill_source_hash(skill_md: Path) -> str | None:
    """Canonical pin hash of ``SKILL.md`` as it stands, or ``None`` if unreadable."""
    try:
        return hash_source(skill_md.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None


def _pack_member_names(folder: Path) -> list[str]:
    """Relative POSIX names of every file or orphaned sidecar target in ``folder``."""
    names = {"SKILL.md"}
    for entry in folder.rglob("*"):
        if entry.is_symlink() or not entry.is_file():
            continue
        relative = entry.relative_to(folder).as_posix()
        names.add(relative.removesuffix(SIDECAR_SUFFIX))
    return sorted(names)


def _pin_name(folder: Path, relative: str, member: Path) -> str:
    if relative == "SKILL.md":
        return pin_name_for_path(member)
    return approval_name(member, f"skills/{folder.name}/{relative}")


def _read_optional(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _restore_optional(path: Path, content: bytes | None) -> None:
    if content is None:
        path.unlink(missing_ok=True)
    else:
        path.write_bytes(content)


__all__ = ["SkillPackError", "revoke_skill_folder", "sign_skill_folder", "skill_pack_files"]
