"""Jailed, signature-verified access to a registered skill's bundled files.

J4 B4/B5. The loader verifies only ``SKILL.md``; a skill's ``references/`` and
``scripts/`` were never checked again after promote, so an edited reference
reached the model and a swapped script ran. This module is the ONE place a
skill's sub-files are read for the model or for execution:

* **Jail** — the path is a clean relative POSIX path (no ``..``, no absolute
  path, no backslash, no ``.arcsig``) and every component is opened with
  no-follow handles from the skill folder, so a symlinked file or folder never
  escapes the bundle (TOCTOU-safe: the inode read is the inode checked).
* **Verify** — the bytes must carry a sidecar signature from an OPERATOR-pinned
  key. The agent's own key never vouches for an operator or imported skill, even
  when it is pinned (agent-key laundering, ASI03/ASI06). Only an agent-authored
  ``workspace`` skill verifies against the agent key, because only the agent's
  dedicated create/update tools may write there.
* **Anchored skills** — a skill on the revision chain is read through the
  revision authority, which checks the external head and the operator-signed
  bundle manifest.
* **Builtins** — the wheel's own skills are TRUSTED; they are jailed but need no
  sidecar (the loader treats that root the same way).

Every refusal raises a :class:`SkillFileError` whose message is safe to show
the model: it names the skill and the relative path, never a host path.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from arctrust import ArtifactSignature, load_validators, verify_artifact

from arcagent.capabilities.artifact_signing import SIDECAR_SUFFIX
from arcagent.capabilities.capability_loader import RootTrust, root_trust

_MAX_FILE_BYTES = 10 * 1024 * 1024
_MAX_SIDECAR_BYTES = 64 * 1024
_MAX_FILES = 512
_MAX_DEPTH = 12
#: Revision bookkeeping that is never a skill file.
_REVISION_FILES = frozenset({"manifest.json", "manifest.json" + SIDECAR_SUFFIX})
#: Scan-root prefix of the agent-authored (workspace) roots.
_WORKSPACE_ROOT = "workspace"


class SkillFileError(Exception):
    """Base for every refusal; the message is safe to show the model."""


class SkillFilePathError(SkillFileError):
    """The relative path is unsafe or does not name a regular file in the bundle."""


class SkillFileIntegrityError(SkillFileError):
    """The file's bytes do not verify against a key allowed for this skill."""


class SkillBundleRef(Protocol):
    """What the verifier needs to know about one registered skill."""

    @property
    def name(self) -> str: ...

    @property
    def location(self) -> Path: ...

    @property
    def scan_root(self) -> str: ...

    @property
    def bundle_folder(self) -> Path | None: ...

    @property
    def read_current(self) -> object | None: ...


class SkillRevisionFiles(Protocol):
    """The revision authority's file seam (anchored skills)."""

    def active_folder(self, folder: Path) -> Path | None: ...

    def read_verified_file(self, folder: Path, relpath: str) -> bytes: ...


@dataclass(frozen=True)
class SkillFiles:
    """Verified, jailed reads of skill bundle files.

    ``operator_keys`` is read per call so an operator pin/unpin takes effect
    without a restart; ``agent_key`` is removed from it unconditionally.
    """

    operator_keys: Callable[[], frozenset[bytes]]
    agent_key: bytes | None = None
    revisions: SkillRevisionFiles | None = None

    def inventory(self, skill: SkillBundleRef) -> tuple[str, ...]:
        """Every regular file in the active bundle, sorted; never a sidecar or link."""
        folder = self._active_folder(skill)
        return tuple(sorted(_walk(folder)))

    def read(self, skill: SkillBundleRef, relpath: str) -> bytes:
        """Return ``relpath``'s bytes only if they verify for this skill."""
        parts = _safe_parts(skill.name, relpath)
        anchor = self._anchor(skill)
        if anchor is not None:
            return self._read_anchored(anchor, skill, relpath)
        folder = self._installed_folder(skill)
        content = _read_no_follow(folder, parts, _MAX_FILE_BYTES, skill.name, relpath)
        if root_trust(skill.scan_root) is RootTrust.TRUSTED:
            return content
        self._verify(skill, folder, parts, content, relpath)
        return content

    def materialize(self, skill: SkillBundleRef, destination: Path) -> Path:
        """Copy the verified bundle to ``destination/skills/<folder>`` and return it.

        Each file is verified as it is read, and only the verified bytes are
        written, so a swap after verification cannot reach execution (the run
        happens in the private copy, not in the agent-reachable original).
        Sidecars are copied beside the data so a downstream re-verify agrees.
        """
        folder = self._active_folder(skill)
        copy = destination / "skills" / self._installed_folder(skill).name
        copy.mkdir(parents=True, mode=0o700)
        for relpath in sorted(_walk(folder)):
            data = self.read(skill, relpath)
            target = copy / relpath
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            target.write_bytes(data)
            sidecar = _optional_sidecar(folder, PurePosixPath(relpath).parts)
            if sidecar is not None:
                target.with_name(target.name + SIDECAR_SUFFIX).write_bytes(sidecar)
        return copy

    def keys_for(self, scan_root: str) -> frozenset[bytes]:
        """Keys whose signature makes a file in ``scan_root`` trusted."""
        agent = frozenset() if self.agent_key is None else frozenset({self.agent_key})
        operator = self.operator_keys() - agent
        if scan_root.startswith(_WORKSPACE_ROOT):
            return operator | agent
        return operator

    def _anchor(self, skill: SkillBundleRef) -> SkillRevisionFiles | None:
        """The revision authority when ``skill`` is on the revision chain."""
        return self.revisions if skill.read_current is not None else None

    def _installed_folder(self, skill: SkillBundleRef) -> Path:
        return skill.bundle_folder or skill.location.parent

    def _active_folder(self, skill: SkillBundleRef) -> Path:
        installed = self._installed_folder(skill)
        anchor = self._anchor(skill)
        if anchor is None:
            return installed
        try:
            active = anchor.active_folder(installed)
        except (OSError, ValueError) as exc:
            raise SkillFileIntegrityError(
                f"skill {skill.name!r}: revision authority unavailable"
            ) from exc
        return active if active is not None else installed

    def _read_anchored(
        self, anchor: SkillRevisionFiles, skill: SkillBundleRef, relpath: str
    ) -> bytes:
        try:
            return anchor.read_verified_file(self._installed_folder(skill), relpath)
        except (OSError, ValueError) as exc:
            raise SkillFileIntegrityError(
                f"skill {skill.name!r}: {relpath} failed revision verification"
            ) from exc

    def _verify(
        self,
        skill: SkillBundleRef,
        folder: Path,
        parts: tuple[str, ...],
        content: bytes,
        relpath: str,
    ) -> None:
        raw = _optional_sidecar(folder, parts)
        refusal = SkillFileIntegrityError(
            f"skill {skill.name!r}: {relpath} is not signed by a trusted operator key "
            "(missing signature, changed after signing, or signed by a key that may "
            "not approve this skill)"
        )
        if raw is None:
            raise refusal
        try:
            signature = ArtifactSignature.from_json(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise refusal from exc
        if signature is None or not any(
            verify_artifact(content, signature, trusted_public_key=key)
            for key in self.keys_for(skill.scan_root)
        ):
            raise refusal


def operator_keys_from(config_path: Path) -> frozenset[bytes]:
    """The operator keys pinned in an agent's ``[security.validators]`` (read fresh).

    A malformed or unreadable entry contributes nothing (fail closed: the file
    that needed it stays refused) rather than raising into the tool call.
    """
    try:
        encoded = load_validators(config_path).trusted_keys
    except (OSError, ValueError):
        return frozenset()
    keys: set[bytes] = set()
    for entry in encoded:
        try:
            keys.add(bytes.fromhex(entry))
        except ValueError:
            continue
    return frozenset(keys)


def _safe_parts(skill_name: str, relpath: str) -> tuple[str, ...]:
    """Split ``relpath`` into clean components or refuse it."""
    refusal = SkillFilePathError(
        f"skill {skill_name!r}: {relpath!r} is not a file path inside the skill"
    )
    if not relpath or "\\" in relpath or "\x00" in relpath or relpath.startswith("/"):
        raise refusal
    path = PurePosixPath(relpath)
    parts = path.parts
    if (
        not parts
        or len(parts) > _MAX_DEPTH
        or any(part in {"", ".", ".."} for part in parts)
        or str(path) != relpath
        or relpath.endswith(SIDECAR_SUFFIX)
    ):
        raise refusal
    return parts


def _open_dir_chain(folder: Path, parts: tuple[str, ...]) -> int:
    """Open ``folder`` then each sub-directory in ``parts`` without following links."""
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(folder, flags)
    try:
        for part in parts:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_regular(directory_fd: int, name: str, limit: int) -> bytes:
    """Read one regular, single-link file under ``directory_fd`` (no-follow)."""
    before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
        raise OSError("not a regular single-link file within limits")
    fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
    try:
        status = os.fstat(fd)
        if (status.st_dev, status.st_ino) != (before.st_dev, before.st_ino):
            raise OSError("file changed while opening")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > limit:
            raise OSError("file exceeds limit")
        return data
    finally:
        os.close(fd)


def _read_no_follow(
    folder: Path, parts: tuple[str, ...], limit: int, skill_name: str, relpath: str
) -> bytes:
    try:
        directory_fd = _open_dir_chain(folder, parts[:-1])
    except OSError as exc:
        raise SkillFilePathError(
            f"skill {skill_name!r}: {relpath!r} is not a file inside the skill"
        ) from exc
    try:
        return _read_regular(directory_fd, parts[-1], limit)
    except OSError as exc:
        raise SkillFilePathError(
            f"skill {skill_name!r}: {relpath!r} is not a regular file inside the skill"
        ) from exc
    finally:
        os.close(directory_fd)


def _optional_sidecar(folder: Path, parts: tuple[str, ...]) -> bytes | None:
    """The sidecar bytes beside ``parts`` (no-follow), or None when absent/unsafe."""
    try:
        directory_fd = _open_dir_chain(folder, parts[:-1])
    except OSError:
        return None
    try:
        return _read_regular(directory_fd, parts[-1] + SIDECAR_SUFFIX, _MAX_SIDECAR_BYTES)
    except OSError:
        return None
    finally:
        os.close(directory_fd)


def _walk(folder: Path) -> list[str]:
    """Relative paths of the regular files under ``folder``; links are skipped."""
    found: list[str] = []
    try:
        root_fd = _open_dir_chain(folder, ())
    except OSError:
        return found

    def visit(directory_fd: int, prefix: str, depth: int) -> None:
        if depth > _MAX_DEPTH:
            return
        with os.scandir(directory_fd) as entries:
            names = sorted(entry.name for entry in entries)
        for name in names:
            if len(found) >= _MAX_FILES:
                return
            relative = f"{prefix}{name}"
            status = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(status.st_mode):
                child = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_fd,
                )
                try:
                    visit(child, relative + "/", depth + 1)
                finally:
                    os.close(child)
            elif (
                stat.S_ISREG(status.st_mode)
                and not name.endswith(SIDECAR_SUFFIX)
                and not (prefix == "" and name in _REVISION_FILES)
            ):
                found.append(relative)

    try:
        visit(root_fd, "", 1)
    finally:
        os.close(root_fd)
    return found


__all__ = [
    "SkillBundleRef",
    "SkillFileError",
    "SkillFileIntegrityError",
    "SkillFilePathError",
    "SkillFiles",
    "SkillRevisionFiles",
    "operator_keys_from",
]
