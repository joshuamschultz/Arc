"""Immutable, operator-signed skill revisions activated by an external anchor."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import mkdtemp
from typing import Any, Protocol

from arctrust import (
    AnchorHead,
    ArtifactSignature,
    MonotonicAnchor,
    Signer,
    hash_source,
    load_validators,
    verify_artifact,
)

from arcagent.capabilities.artifact_signing import write_signature_with_signer
from arcagent.capabilities.capability_registry import SkillEntry
from arcagent.capabilities.skill_validator import validate_skill_folder
from arcagent.modules.capability_import.service import approval_name, approval_source

_MAX_FILES = 512
_MAX_ENTRIES = 512
_MAX_DEPTH = 12
_MAX_TOTAL = 200 * 1024 * 1024
_MAX_FILE = 32 * 1024 * 1024


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _scope(agent_did: str, skill_name: str) -> str:
    return f"skill/{_digest(agent_did.encode())}/{skill_name}"


def _safe_name(name: str) -> bool:
    return name.replace("-", "_").isidentifier() and not name.startswith(("-", "_"))


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _open_dir(path: Path) -> int:
    """Pin every ancestor without following substituted links."""
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise RuntimeError("secure skill reads are unavailable on this platform")
    absolute = path.absolute()
    descriptor = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in absolute.parts[1:]:
            next_descriptor = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _snapshot(folder: Path) -> dict[str, bytes]:
    """Capture a bounded bundle through directory-relative no-follow handles."""
    files: dict[str, bytes] = {}
    total = 0
    entries = 0
    root_fd = _open_dir(folder)
    try:

        def walk(directory_fd: int, prefix: str, depth: int) -> None:
            nonlocal total, entries
            if depth > _MAX_DEPTH:
                raise ValueError("skill bundle exceeds resource limits")
            names: list[str] = []
            with os.scandir(directory_fd) as children:
                for child in children:
                    entries += 1
                    if entries > _MAX_ENTRIES:
                        raise ValueError("skill bundle exceeds resource limits")
                    names.append(child.name)
            for name in sorted(names):
                if name in {"", ".", ".."} or "/" in name:
                    raise ValueError("skill bundle has an unsafe path")
                relative = f"{prefix}{name}"
                before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if not (stat.S_ISDIR(before.st_mode) or stat.S_ISREG(before.st_mode)):
                    raise ValueError("skill bundle has a linked or special file")
                if stat.S_ISREG(before.st_mode) and before.st_nlink != 1:
                    raise ValueError("skill bundle has a linked or special file")
                fd = os.open(
                    name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
                )
                try:
                    status = os.fstat(fd)
                    if (status.st_dev, status.st_ino) != (before.st_dev, before.st_ino):
                        raise ValueError("skill bundle changed while reading")
                    if stat.S_ISDIR(status.st_mode):
                        walk(fd, relative + "/", depth + 1)
                        continue
                    if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
                        raise ValueError("skill bundle has a linked or special file")
                    total += status.st_size
                    if (
                        status.st_size > _MAX_FILE
                        or total > _MAX_TOTAL
                        or len(files) >= _MAX_FILES
                    ):
                        raise ValueError("skill bundle exceeds resource limits")
                    chunks: list[bytes] = []
                    remaining = status.st_size + 1
                    while remaining > 0:
                        chunk = os.read(fd, min(64 * 1024, remaining))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        remaining -= len(chunk)
                    content = b"".join(chunks)
                    if len(content) != status.st_size:
                        raise ValueError("skill bundle changed while reading")
                    files[relative] = content
                finally:
                    os.close(fd)

        walk(root_fd, "", 1)
    finally:
        os.close(root_fd)
    return files


def _file_rows(files: dict[str, bytes]) -> list[dict[str, object]]:
    return [
        {"path": path, "sha256": _digest(content), "size": len(content)}
        for path, content in sorted(files.items())
        if path not in {"manifest.json", "manifest.json.arcsig"}
    ]


def reviewed_bundle_digest(files: Mapping[str, bytes]) -> str:
    """Hash the exact reviewed data files without local signature sidecars."""
    return _digest(_canonical({"files": _file_rows(dict(files))}))


def _validate_reviewed_files(files: Mapping[str, bytes]) -> None:
    if "SKILL.md" not in files or len(files) > _MAX_FILES:
        raise ValueError("reviewed skill bundle is incomplete or exceeds limits")
    total = 0
    for relative, content in files.items():
        path = PurePosixPath(relative)
        if (
            not isinstance(content, bytes)
            or str(path) != relative
            or path.is_absolute()
            or "\\" in relative
            or any(part in {"", ".", ".."} for part in path.parts)
            or (relative != "SKILL.md" and path.parts[0] not in {"references", "assets", "evals"})
            or relative.endswith(".arcsig")
            or len(content) > _MAX_FILE
        ):
            raise ValueError("reviewed skill bundle has an unsafe file")
        total += len(content)
        if total > _MAX_TOTAL:
            raise ValueError("reviewed skill bundle exceeds limits")


@dataclass(frozen=True)
class ReviewedSkillBundle:
    """Operator-approved complete bundle and the active head it replaces."""

    files: Mapping[str, bytes]
    reviewed_sha256: str
    expected_active_digest: str


class SkillRuntime(Protocol):
    """Canonical live-agent reload and registered skill read seam."""

    @property
    def skills(self) -> Sequence[SkillEntry]: ...

    async def reload_or_raise(self) -> object: ...


def _durable(folder: Path) -> None:
    for path in sorted(folder.rglob("*")):
        if path.is_file():
            with path.open("rb") as source:
                os.fsync(source.fileno())
    for path in sorted((p for p in folder.rglob("*") if p.is_dir()), reverse=True):
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    _fsync_dir(folder)


def _fsync_dir(folder: Path) -> None:
    descriptor = _open_dir(folder)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_small_regular(folder: Path, name: str) -> bytes:
    directory_fd = _open_dir(folder)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        try:
            status = os.fstat(fd)
            if (
                not stat.S_ISREG(status.st_mode)
                or status.st_nlink != 1
                or status.st_size > 1024 * 1024
            ):
                raise ValueError("skill revision evidence is unsafe")
            data = os.read(fd, status.st_size + 1)
            if len(data) != status.st_size:
                raise ValueError("skill revision evidence changed")
            return data
        finally:
            os.close(fd)
    finally:
        os.close(directory_fd)


def _reject_regressed_head(
    folder: Path, head: AnchorHead, config_path: Path, agent_did: str
) -> None:
    revisions = folder.parent.parent / ".skill-revisions" / folder.name
    if not revisions.exists():
        return
    trusted_keys = load_validators(config_path).trusted_keys
    directory_fd = _open_dir(revisions)
    try:
        with os.scandir(directory_fd) as entries:
            names = [entry.name for entry in entries]
    finally:
        os.close(directory_fd)
    if len(names) > _MAX_ENTRIES:
        raise ValueError("skill revision history exceeds resource limits")
    for name in names:
        if name == head.digest or not re.fullmatch(r"[0-9a-f]{64}", name):
            continue
        candidate = revisions / name
        manifest_bytes = _read_small_regular(candidate, "manifest.json")
        signature_bytes = _read_small_regular(candidate, "manifest.json.arcsig")
        signature = ArtifactSignature.from_json(signature_bytes.decode("utf-8"))
        manifest = json.loads(manifest_bytes)
        if (
            not isinstance(manifest, dict)
            or signature is None
            or signature.public_key not in trusted_keys
            or not verify_artifact(manifest_bytes, signature)
            or _digest(manifest_bytes) != name
            or manifest.get("agent_did") != agent_did
            or manifest.get("skill_name") != folder.name
        ):
            raise ValueError("skill revision evidence is invalid")
        version = manifest.get("activation_version")
        if isinstance(version, int) and not isinstance(version, bool) and version > head.version:
            raise ValueError("skill revision authority regressed")


class AnchoredSkillRevisionResolver:
    """Resolve the externally anchored head; never trust a local pointer alone."""

    def __init__(
        self,
        *,
        agent_did: str,
        config_path: Path,
        anchor_factory: Callable[[str, str], MonotonicAnchor],
    ) -> None:
        self._agent_did = agent_did
        self._config_path = config_path
        self._anchor_factory = anchor_factory

    def _anchor(self, skill_name: str) -> MonotonicAnchor:
        anchor = self._anchor_factory(self._agent_did, skill_name)
        if anchor.scope != _scope(self._agent_did, skill_name):
            raise ValueError("skill anchor scope does not match agent and skill")
        return anchor

    def resolve(self, folder: Path, scan_root: str) -> Path:
        """Return an immutable active SKILL.md, or None before first revision."""
        if not _safe_name(folder.name):
            raise ValueError("invalid skill name")
        anchor = self._anchor(folder.name)
        head = anchor.latest()
        if head is None:
            raise ValueError("skill revision authority is unavailable")
        _reject_regressed_head(folder, head, self._config_path, self._agent_did)
        revision = folder.parent.parent / ".skill-revisions" / folder.name / head.digest
        self._verify_revision(revision, head, folder.name)
        return revision / "SKILL.md"

    def preview_original(self, folder: Path) -> str:
        """Read a signed, approved original for explicit first activation."""
        if self._anchor(folder.name).latest() is not None:
            raise ValueError("skill already has an active revision")
        revisions_root = folder.parent.parent / ".skill-revisions" / folder.name
        if revisions_root.exists() and any(revisions_root.iterdir()):
            raise ValueError("skill revision authority is unavailable after reset")
        files = _snapshot(folder)
        self._verify_original(folder, files)
        return files["SKILL.md"].decode("utf-8")

    def read_current(self, folder: Path, path: Path) -> str | None:
        """Re-check current external head and every bundle byte before use."""
        head = self._anchor(folder.name).latest()
        if head is None:
            raise ValueError("skill revision authority is unavailable")
        _reject_regressed_head(folder, head, self._config_path, self._agent_did)
        revision = folder.parent.parent / ".skill-revisions" / folder.name / head.digest
        if path != revision / "SKILL.md":
            return None
        return self._verify_revision(revision, head, folder.name).decode("utf-8")

    def read_bundle(self, folder: Path) -> dict[str, bytes]:
        """Return the exact verified current bundle for an operator edit."""
        head = self._anchor(folder.name).latest()
        if head is None:
            raise ValueError("skill revision authority is unavailable")
        _reject_regressed_head(folder, head, self._config_path, self._agent_did)
        revision = folder.parent.parent / ".skill-revisions" / folder.name / head.digest
        files = _snapshot(revision)
        self._verify_revision(revision, head, folder.name, files=files)
        return files

    def revision_history(self, folder: Path) -> list[tuple[str, int, str, bool]]:
        """Return verified activation lineage from the external head backward."""
        head = self._anchor(folder.name).latest()
        if head is None:
            raise ValueError("skill revision authority is unavailable")
        _reject_regressed_head(folder, head, self._config_path, self._agent_did)
        history: list[tuple[str, int, str, bool]] = []
        while head is not None:
            if len(history) >= _MAX_ENTRIES or not re.fullmatch(r"[0-9a-f]{64}", head.digest):
                raise ValueError("active skill history is invalid")
            revision = folder.parent.parent / ".skill-revisions" / folder.name / head.digest
            files = _snapshot(revision)
            body = self._verify_revision(revision, head, folder.name, files=files)
            history.append((head.digest, head.version, body.decode("utf-8"), len(history) == 0))
            if head.previous_digest is None:
                if head.version != 1:
                    raise ValueError("active skill history is incomplete")
                break
            if head.version <= 1 or not re.fullmatch(r"[0-9a-f]{64}", head.previous_digest):
                raise ValueError("active skill history is invalid")
            prior_folder = (
                folder.parent.parent / ".skill-revisions" / folder.name / head.previous_digest
            )
            prior_files = _snapshot(prior_folder)
            prior_manifest = json.loads(prior_files["manifest.json"])
            head = AnchorHead(
                scope=head.scope,
                version=head.version - 1,
                digest=head.previous_digest,
                previous_digest=prior_manifest.get("parent_digest"),
            )
        return history

    def activate_prior(
        self,
        folder: Path,
        revision_digest: str,
        *,
        expected_sha256: str,
        signer: Signer,
        operator_did: str,
    ) -> str:
        """Sign the selected verified body as a new forward activation."""
        target = next(
            (
                body
                for digest, _, body, _ in self.revision_history(folder)
                if digest == revision_digest
            ),
            None,
        )
        if target is None:
            raise ValueError("skill revision not found")
        return self.revise(
            folder,
            target.encode("utf-8"),
            expected_sha256=expected_sha256,
            signer=signer,
            operator_did=operator_did,
            restore_from_digest=revision_digest,
        )

    async def promote_reviewed_bundle(
        self,
        folder: Path,
        proposal: ReviewedSkillBundle,
        *,
        signer: Signer,
        operator_did: str,
        runtime: SkillRuntime,
    ) -> str:
        """Activate reviewed bytes at the expected head and verify live reload."""
        files = dict(proposal.files)
        _validate_reviewed_files(files)
        if reviewed_bundle_digest(files) != proposal.reviewed_sha256:
            raise ValueError("reviewed skill bundle digest changed")
        current = self.read_bundle(folder)
        await asyncio.to_thread(
            self.revise,
            folder,
            files["SKILL.md"],
            expected_sha256=_digest(current["SKILL.md"]),
            expected_active_digest=proposal.expected_active_digest,
            replacement_files=files,
            signer=signer,
            operator_did=operator_did,
        )
        await runtime.reload_or_raise()
        active = self.read_bundle(folder)
        data = {
            path: content
            for path, content in active.items()
            if path not in {"manifest.json", "manifest.json.arcsig"}
            and not path.endswith(".arcsig")
        }
        if data != files:
            raise ValueError("activated skill bundle differs from reviewed content")
        entry = next((item for item in runtime.skills if item.name == folder.name), None)
        if (
            entry is None
            or entry.read_current is None
            or entry.read_current() != files["SKILL.md"].decode("utf-8")
        ):
            raise ValueError("runtime did not load the reviewed skill")
        head = self._anchor(folder.name).latest()
        if head is None:
            raise ValueError("skill revision authority is unavailable")
        return head.digest

    def revise(
        self,
        folder: Path,
        content: bytes,
        *,
        expected_sha256: str,
        signer: Signer,
        operator_did: str,
        resource_updates: dict[str, bytes] | None = None,
        restore_from_digest: str | None = None,
        expected_active_digest: str | None = None,
        replacement_files: Mapping[str, bytes] | None = None,
    ) -> str:
        """Stage a signed complete bundle, then CAS its external active head."""
        if not _safe_name(folder.name) or folder.is_symlink() or not folder.is_dir():
            raise ValueError("skill folder is unsafe")
        if len(content) > _MAX_FILE:
            raise ValueError("skill exceeds file limit")
        anchor = self._anchor(folder.name)
        prior_head = anchor.latest()
        if expected_active_digest is not None and (
            prior_head is None or prior_head.digest != expected_active_digest
        ):
            raise ValueError("active skill revision changed since review")
        if replacement_files is not None and (resource_updates or restore_from_digest):
            raise ValueError("reviewed replacement cannot mix resource updates")
        if replacement_files is not None:
            _validate_reviewed_files(replacement_files)
            if replacement_files["SKILL.md"] != content:
                raise ValueError("reviewed skill body changed")
        if prior_head is not None:
            _reject_regressed_head(folder, prior_head, self._config_path, self._agent_did)
        if prior_head is None:
            revisions_root = folder.parent.parent / ".skill-revisions" / folder.name
            if revisions_root.exists() and any(revisions_root.iterdir()):
                raise ValueError("skill revision authority is unavailable after reset")
        source_folder = (
            folder.parent.parent / ".skill-revisions" / folder.name / prior_head.digest
            if prior_head
            else folder
        )
        source_files = _snapshot(source_folder)
        previous = (
            self._verify_revision(source_folder, prior_head, folder.name, files=source_files)
            if prior_head
            else source_files["SKILL.md"]
        )
        if _digest(previous) != expected_sha256:
            raise ValueError("skill changed since it was opened")
        if prior_head is None:
            self._verify_original(folder, source_files)
        bundle_files = source_files
        if replacement_files is not None:
            bundle_files = dict(replacement_files)
        if restore_from_digest is not None:
            if restore_from_digest not in {
                digest for digest, _, _, _ in self.revision_history(folder)
            }:
                raise ValueError("skill revision not found")
            restored_folder = (
                folder.parent.parent / ".skill-revisions" / folder.name / restore_from_digest
            )
            bundle_files = _snapshot(restored_folder)
            restored_manifest = json.loads(bundle_files["manifest.json"])
            restored_head = AnchorHead(
                scope=anchor.scope,
                version=restored_manifest["activation_version"],
                digest=restore_from_digest,
                previous_digest=restored_manifest["parent_digest"],
            )
            self._verify_revision(restored_folder, restored_head, folder.name, files=bundle_files)
        validators = load_validators(self._config_path)
        if signer.public_key.hex() not in validators.trusted_keys:
            raise ValueError("operator signing key is not approved for this agent")
        revisions = folder.parent.parent / ".skill-revisions" / folder.name
        revisions.mkdir(parents=True, exist_ok=True)
        staging = Path(mkdtemp(prefix=".staging-", dir=revisions))
        try:
            for relative, data in bundle_files.items():
                if relative in {"manifest.json", "manifest.json.arcsig"}:
                    continue
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                if replacement_files is not None and relative != "SKILL.md":
                    write_signature_with_signer(
                        target, data, signer_did=operator_did, signer=signer
                    )
            for relative, data in (resource_updates or {}).items():
                parts = Path(relative).parts
                if (
                    Path(relative).is_absolute()
                    or not parts
                    or parts[0] not in {"references", "assets", "evals"}
                    or any(part in {"", ".", ".."} for part in parts)
                    or relative.endswith(".arcsig")
                    or len(data) > _MAX_FILE
                ):
                    raise ValueError("skill resource update is unsafe")
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                write_signature_with_signer(target, data, signer_did=operator_did, signer=signer)
            skill_md = staging / "SKILL.md"
            skill_md.write_bytes(content)
            validation = validate_skill_folder(staging, "revision")
            if (
                not validation.ok
                or validation.entry is None
                or validation.entry.name != folder.name
            ):
                raise ValueError("edited skill failed validation or changed its name")
            write_signature_with_signer(skill_md, content, signer_did=operator_did, signer=signer)
            expected_version = prior_head.version + 1 if prior_head else 1
            manifest = {
                "agent_did": self._agent_did,
                "skill_name": folder.name,
                "parent_digest": prior_head.digest if prior_head else None,
                "activation_version": expected_version,
                "signer_did": operator_did,
                "signer_public_key": signer.public_key.hex(),
                "files": _file_rows(_snapshot(staging)),
            }
            manifest_bytes = _canonical(manifest)
            manifest_path = staging / "manifest.json"
            manifest_path.write_bytes(manifest_bytes)
            write_signature_with_signer(
                manifest_path, manifest_bytes, signer_did=operator_did, signer=signer
            )
            revision_digest = _digest(manifest_bytes)
            final = revisions / revision_digest
            _durable(staging)
            if not final.exists():
                os.replace(staging, final)
                _fsync_dir(revisions)
            expected_head = AnchorHead(
                scope=anchor.scope,
                version=expected_version,
                digest=revision_digest,
                previous_digest=prior_head.digest if prior_head else None,
            )
            self._verify_revision(final, expected_head, folder.name)
            try:
                activated: AnchorHead | None = anchor.compare_and_advance(
                    prior_head, revision_digest, f"activate:{folder.name}:{operator_did}"
                )
            except Exception:
                activated = anchor.latest()
                if (
                    activated is None
                    or activated.digest != revision_digest
                    or activated.version != expected_version
                ):
                    raise
            if (
                activated is None
                or activated.digest != revision_digest
                or activated.version != expected_version
            ):
                raise ValueError("skill anchor returned an unexpected head")
            return _digest(content)
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def _verify_original(self, folder: Path, files: dict[str, bytes]) -> None:
        validators = load_validators(self._config_path)
        data_paths = {path for path in files if not path.endswith(".arcsig")}
        if "SKILL.md" not in data_paths or {
            path for path in files if path.endswith(".arcsig")
        } != {path + ".arcsig" for path in data_paths}:
            raise ValueError("installed skill bundle is not currently trusted")
        for relative in data_paths:
            content = files[relative]
            try:
                signature = ArtifactSignature.from_json(
                    files[relative + ".arcsig"].decode("utf-8")
                )
            except (UnicodeError, ValueError) as exc:
                raise ValueError("installed skill bundle is not currently trusted") from exc
            name = approval_name(folder / relative, f"skills/{folder.name}/{relative}")
            if (
                signature is None
                or signature.public_key not in validators.trusted_keys
                or not verify_artifact(content, signature)
                or not any(
                    item.name == name and item.hash == hash_source(approval_source(content))
                    for item in validators.approved
                )
            ):
                raise ValueError("installed skill bundle is not currently trusted")

    def _verify_revision(
        self,
        folder: Path,
        head: AnchorHead,
        skill_name: str,
        *,
        files: dict[str, bytes] | None = None,
    ) -> bytes:
        files = files if files is not None else _snapshot(folder)
        manifest_bytes = files.get("manifest.json", b"")
        if len(manifest_bytes) > 1024 * 1024 or _digest(manifest_bytes) != head.digest:
            raise ValueError("active revision manifest changed")
        manifest = json.loads(manifest_bytes)
        signature = ArtifactSignature.from_json(files["manifest.json.arcsig"].decode("utf-8"))
        validators = load_validators(self._config_path)
        if (
            signature is None
            or signature.public_key not in validators.trusted_keys
            or signature.signer_did != manifest.get("signer_did")
            or signature.public_key != manifest.get("signer_public_key")
            or not verify_artifact(manifest_bytes, signature)
            or manifest.get("agent_did") != self._agent_did
            or manifest.get("skill_name") != skill_name
            or manifest.get("activation_version") != head.version
            or manifest.get("parent_digest") != head.previous_digest
        ):
            raise ValueError("active revision approval is invalid")
        listed = manifest.get("files")
        if not isinstance(listed, list) or listed != _file_rows(files):
            raise ValueError("active revision bundle changed")
        return files["SKILL.md"]
