"""Review, trust, and revoke agent-scoped capability imports."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from datetime import UTC, datetime
from pathlib import Path
from tempfile import mkdtemp

from arctrust import (
    AuditEvent,
    AuditSink,
    Signer,
    approve,
    emit,
    load_validators,
    persist_validators,
    pin_key,
)
from pydantic import ValidationError

from arcagent.capabilities.artifact_signing import write_signature_with_signer
from arcagent.capabilities.capability_loader import pin_name_for_path
from arcagent.capabilities.import_trust import revoke_import_artifact as revoke_capability
from arcagent.modules.capability_import.errors import CapabilityImportError
from arcagent.modules.capability_import.ledger import ImportLedger
from arcagent.modules.capability_import.manifest import (
    build_manifest,
    review_digest,
    verify_manifest,
    write_evidence,
)
from arcagent.modules.capability_import.models import (
    CapabilityImportFile,
    CapabilityImportLimits,
    CapabilityImportManifest,
    CapabilityImportResult,
    CapabilityImportReview,
    CapabilityImportStatus,
)

_MAX_REVIEW_BYTES = 1024 * 1024
_MANIFEST_FIELDS = frozenset(
    {
        "archive_sha256",
        "files",
        "findings",
        "import_id",
        "limits",
        "review_digest",
        "skills",
        "supplier_metadata",
        "supplier_sbom_sha256",
        "target_agent_did",
        "tools",
    }
)


class CapabilityImportService:
    """Create review evidence, promote signed capabilities, and track drift."""

    def __init__(self, capabilities_root: Path, *, audit_sink: AuditSink | None = None) -> None:
        self._root = Path(capabilities_root)
        self._audit_sink = audit_sink
        self._ledger = ImportLedger(self._root, audit_sink=audit_sink)

    def list_reviews(self) -> list[CapabilityImportReview]:
        """Return metadata-only review rows for this agent's staged imports.

        Staging content is executable code and remains quarantined. The UI gets
        only the signed review metadata and lifecycle state; it cannot turn a
        listing request into a source read or an activation.
        """
        staging_root = self._root / "imports" / ".staging"
        if not staging_root.is_dir():
            return []
        rows: list[CapabilityImportReview] = []
        try:
            import_dirs = sorted(staging_root.iterdir())
        except OSError:
            return rows
        for import_dir in import_dirs:
            manifest_path = import_dir / "import.json"
            if not import_dir.is_dir() or not manifest_path.is_file():
                continue
            try:
                if manifest_path.stat().st_size > _MAX_REVIEW_BYTES:
                    continue
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if not isinstance(manifest, dict):
                continue
            if manifest.get("import_id") != import_dir.name:
                continue
            if not set(manifest).issubset(_MANIFEST_FIELDS):
                continue
            supplier = manifest.get("supplier_metadata")
            if not isinstance(supplier, dict):
                continue
            try:
                ledger = self._ledger.get(import_dir.name)
                if ledger is not None and not isinstance(ledger, dict):
                    continue
                status = CapabilityImportStatus(
                    str(ledger.get("status", CapabilityImportStatus.QUARANTINED.value))
                    if ledger
                    else CapabilityImportStatus.QUARANTINED.value
                )
                if status in {
                    CapabilityImportStatus.REVIEW_READY,
                    CapabilityImportStatus.PROMOTED,
                } and not self._review_matches(
                    self._manifest_from_staging(import_dir), import_dir
                ):
                    status = CapabilityImportStatus.MODIFIED
                rows.append(self._review_from_payload(manifest, status=status))
            except (RuntimeError, TypeError, ValueError, ValidationError):
                continue
        return rows

    def review_summary(
        self,
        manifest: CapabilityImportManifest,
        *,
        status: CapabilityImportStatus = CapabilityImportStatus.REVIEW_READY,
    ) -> CapabilityImportReview:
        """Return the strict metadata contract for a freshly reviewed import."""
        return self._review_from_payload(manifest.__dict__, status=status)

    @staticmethod
    def _review_from_payload(
        payload: dict[str, object], *, status: CapabilityImportStatus
    ) -> CapabilityImportReview:
        supplier = payload.get("supplier_metadata")
        if not isinstance(supplier, dict):
            raise ValueError("supplier metadata must be an object")
        files = payload.get("files")
        if not isinstance(files, (list, tuple)):
            raise ValueError("review files must be a sequence")
        tools = payload.get("tools")
        skills = payload.get("skills")
        if not isinstance(tools, (list, tuple)) or not isinstance(skills, (list, tuple)):
            raise ValueError("review capabilities must be sequences")
        normalized = {
            "import_id": payload.get("import_id"),
            "status": status,
            "target_agent_did": payload.get("target_agent_did"),
            "archive_sha256": payload.get("archive_sha256"),
            "review_digest": payload.get("review_digest"),
            "files": [item.__dict__ if hasattr(item, "__dict__") else item for item in files],
            "tools": list(tools),
            "skills": list(skills),
            "supplier_sbom_sha256": payload.get("supplier_sbom_sha256"),
            "supplier_metadata_keys": list(supplier),
            "activation": "review_only",
        }
        return CapabilityImportReview.model_validate(normalized)

    def review(
        self,
        intake: CapabilityImportResult,
        *,
        target_agent_did: str,
        limits: CapabilityImportLimits,
    ) -> CapabilityImportManifest:
        manifest = build_manifest(
            intake.staging_dir,
            import_id=intake.import_id,
            target_agent_did=target_agent_did,
            archive_sha256=intake.archive_sha256,
            limits=limits,
        )
        write_evidence(intake.staging_dir, manifest)
        self._ledger.set(
            intake.import_id,
            CapabilityImportStatus.REVIEW_READY,
            review_digest=manifest.review_digest,
            target_agent_did=target_agent_did,
        )
        return manifest

    def status(
        self, manifest: CapabilityImportManifest, staging_dir: Path
    ) -> CapabilityImportStatus:
        """Return review state, marking the ledger modified when bytes drift."""
        if verify_manifest(manifest, staging_dir):
            row = self._ledger.get(manifest.import_id)
            if row is None:
                return CapabilityImportStatus.QUARANTINED
            return CapabilityImportStatus(str(row["status"]))
        self._ledger.set(manifest.import_id, CapabilityImportStatus.MODIFIED)
        return CapabilityImportStatus.MODIFIED

    def read_reviewed_file(self, staging_dir: Path, relative_path: str) -> bytes:
        """Read one reviewed capability file without exposing arbitrary staging files.

        The manifest is the authority for what a caller may read.  In particular,
        this method never accepts ``import.json``/BOM paths or a path that was
        added after review, and it refuses on any byte drift before returning
        source to a UI or CLI editor.
        """
        manifest = self._manifest_from_staging(staging_dir)
        if not self._review_matches(manifest, staging_dir):
            raise ValueError("capability import changed after review")
        if not _is_safe_review_path(relative_path) or not _is_capability(relative_path):
            raise ValueError("capability file path is unsafe")
        if relative_path not in {item.path for item in manifest.files}:
            raise ValueError("capability file is not in the reviewed manifest")
        source = Path(staging_dir) / relative_path
        _reject_symlinked_parents(source, Path(staging_dir))
        if source.is_symlink() or not source.is_file():
            raise ValueError("capability file is unavailable")
        content = source.read_bytes()
        if len(content) > manifest.limits.max_file_bytes:
            raise ValueError("capability file exceeds configured limit")
        return content

    def edit_reviewed_file(
        self,
        staging_dir: Path,
        relative_path: str,
        content: bytes,
        *,
        target_agent_did: str,
        limits: CapabilityImportLimits | None = None,
    ) -> CapabilityImportManifest:
        """Atomically edit one staged file and regenerate its review evidence.

        Editing is an explicit review transition: only a ``REVIEW_READY`` import
        can be edited, the path must already be in the manifest, and static
        validation must pass before the new manifest replaces the old one.  An
        edit never signs or promotes code.
        """
        manifest = self._manifest_from_staging(staging_dir)
        if manifest.target_agent_did != target_agent_did:
            raise ValueError("capability import targets a different agent")
        row = self._ledger.get(manifest.import_id)
        if row is None or row.get("status") != CapabilityImportStatus.REVIEW_READY.value:
            raise ValueError("capability import is not review-ready")
        if not self._review_matches(manifest, staging_dir):
            self._ledger.set(manifest.import_id, CapabilityImportStatus.MODIFIED)
            raise ValueError("capability import changed after review")
        if not _is_safe_review_path(relative_path) or not _is_capability(relative_path):
            raise ValueError("capability file path is unsafe")
        if relative_path not in {item.path for item in manifest.files}:
            raise ValueError("capability file is not in the reviewed manifest")
        effective_limits = limits or manifest.limits
        if len(content) > effective_limits.max_file_bytes:
            raise ValueError("capability file exceeds configured limit")
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("capability files must be UTF-8") from exc
        source = Path(staging_dir) / relative_path
        _reject_symlinked_parents(source, Path(staging_dir))
        if source.is_symlink() or not source.is_file():
            raise ValueError("capability file is unavailable")
        previous = source.read_bytes()
        mode = stat.S_IMODE(source.stat().st_mode)
        temporary = source.with_name(f".{source.name}.edit.tmp")
        try:
            temporary.write_bytes(content)
            temporary.chmod(0o600)
            os.replace(temporary, source)
            try:
                updated = build_manifest(
                    Path(staging_dir),
                    import_id=manifest.import_id,
                    target_agent_did=manifest.target_agent_did,
                    archive_sha256=manifest.archive_sha256,
                    limits=effective_limits,
                )
            except CapabilityImportError as exc:
                raise ValueError("edited capability failed static validation") from exc
            write_evidence(Path(staging_dir), updated)
        except Exception:
            temporary.unlink(missing_ok=True)
            source.write_bytes(previous)
            source.chmod(mode)
            raise
        self._ledger.set(
            manifest.import_id,
            CapabilityImportStatus.REVIEW_READY,
            review_digest=updated.review_digest,
            target_agent_did=target_agent_did,
        )
        return updated

    def promote(
        self,
        staging_dir: Path,
        *,
        target_agent_did: str,
        operator_did: str,
        signer: Signer,
        config_path: Path,
        audit_sink: AuditSink | None = None,
    ) -> tuple[Path, ...]:
        """Sign and copy a reviewed import into this agent's capability roots.

        Promotion is deliberately an operator operation: the supplied signer
        creates the loader's detached signatures and the operator DID is stored
        in the TOFU approvals. Staging must still match its reviewed manifest;
        this method never imports or executes staged source.
        """
        manifest = self._manifest_from_staging(staging_dir)
        if manifest.target_agent_did != target_agent_did:
            raise ValueError("capability import targets a different agent")
        row = self._ledger.get(manifest.import_id)
        if row is None or row.get("status") != CapabilityImportStatus.REVIEW_READY.value:
            raise ValueError("capability import is not review-ready")
        self._require_current_review(manifest, staging_dir, row)

        final_targets = self._target_paths(manifest, self._root)
        for path in final_targets:
            _reject_symlinked_parents(path, self._root)
        if self._root.is_symlink():
            raise ValueError("capability root may not be a symlink")
        validators_before = load_validators(config_path)
        row_before = dict(row)
        temporary = Path(mkdtemp(prefix=f"promotion-{manifest.import_id}-", dir=self._root))
        targets = self._target_paths(manifest, temporary)
        moved: list[Path] = []
        try:
            self._stage_signed_files(manifest, staging_dir, temporary, signer, operator_did)
            if any(
                path.exists() or path.with_name(path.name + ".arcsig").exists()
                for path in final_targets
            ):
                raise ValueError("a promoted capability already exists")
            for temporary_path, final_path in zip(targets, final_targets, strict=True):
                final_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.replace(temporary_path, final_path)
                moved.append(final_path)
                sidecar = temporary_path.with_name(temporary_path.name + ".arcsig")
                final_sidecar = final_path.with_name(final_path.name + ".arcsig")
                os.replace(sidecar, final_sidecar)
                moved.append(final_sidecar)
            pin_key(config_path, public_key=signer.public_key)
            for path in final_targets:
                approve(
                    config_path,
                    name=pin_name_for_path(path),
                    source=path.read_text(encoding="utf-8"),
                    approver=operator_did,
                    timestamp=datetime.now(UTC).isoformat(),
                )
            self._ledger.set(
                manifest.import_id,
                CapabilityImportStatus.PROMOTED,
                target_agent_did=target_agent_did,
                review_digest=manifest.review_digest,
                promoted_paths=[path.relative_to(self._root).as_posix() for path in final_targets],
            )
        except Exception:
            rollback_error: Exception | None = None
            try:
                persist_validators(config_path, validators_before)
            except Exception as error:
                rollback_error = error
            finally:
                for path in reversed(moved):
                    path.unlink(missing_ok=True)
                    _remove_empty_parents(path, self._root)
            try:
                self._ledger.restore(manifest.import_id, row_before)
            except Exception as error:
                rollback_error = rollback_error or error
            if rollback_error is not None:
                raise RuntimeError("capability promotion rollback failed") from rollback_error
            raise
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

        self._emit(
            manifest.import_id,
            "capability_import.promoted",
            operator_did,
            manifest.review_digest,
            audit_sink,
        )
        return tuple(final_targets)

    def revoke(
        self,
        staging_dir: Path,
        *,
        operator_did: str,
        config_path: Path,
        audit_sink: AuditSink | None = None,
    ) -> None:
        """Withdraw trust and remove every artifact promoted from an import."""
        manifest = self._manifest_from_staging(staging_dir)
        row = self._ledger.get(manifest.import_id)
        if row is None or row.get("status") != CapabilityImportStatus.PROMOTED.value:
            raise ValueError("capability import is not promoted")
        self._require_current_review(manifest, staging_dir, row)
        raw_paths = row.get("promoted_paths")
        if not isinstance(raw_paths, list) or any(not isinstance(item, str) for item in raw_paths):
            raise ValueError("capability promotion ledger is malformed")
        paths = [self._root / item for item in raw_paths]
        for path in paths:
            if not _is_under(path, self._root) or path.suffix == ".arcsig" or path.is_symlink():
                raise ValueError("capability promotion ledger contains an unsafe path")
            _reject_symlinked_parents(path, self._root)
            if any(candidate.is_symlink() for candidate in _with_sidecar(path)):
                raise ValueError("capability promotion ledger contains a symlink")
        validators_before = load_validators(config_path)
        row_before = dict(row)
        snapshots = [_snapshot(path) for path in paths for path in _with_sidecar(path)]
        try:
            for path in paths:
                revoke_capability(
                    path,
                    config_path=config_path,
                    operator_did=operator_did,
                    audit_sink=None,
                )
                path.unlink(missing_ok=True)
                _remove_empty_parents(path, self._root)
            self._ledger.set(manifest.import_id, CapabilityImportStatus.REVOKED)
        except Exception:
            rollback_error: Exception | None = None
            try:
                persist_validators(config_path, validators_before)
            except Exception as error:
                rollback_error = error
            try:
                for path, content, mode in snapshots:
                    _restore(path, content, mode)
            except Exception as error:
                rollback_error = rollback_error or error
            try:
                self._ledger.restore(manifest.import_id, row_before)
            except Exception as error:
                rollback_error = rollback_error or error
            if rollback_error is not None:
                raise RuntimeError("capability revocation rollback failed") from rollback_error
            raise
        self._emit(manifest.import_id, "capability_import.revoked", operator_did, None, audit_sink)

    def _require_current_review(
        self,
        manifest: CapabilityImportManifest,
        staging_dir: Path,
        row: dict[str, object],
    ) -> None:
        """Refuse trust mutations when the reviewed evidence is stale.

        The staged manifest is untrusted input too: accepting a rewritten
        ``import.json`` or changed source bytes would let a caller promote one
        thing while the operator reviewed another. Marking the import modified
        makes the refusal visible to every surface and keeps the next action
        explicit (edit/review again), while preserving promotion metadata for
        forensic inspection.
        """
        current = (
            row.get("review_digest") == manifest.review_digest
            and review_digest(manifest) == manifest.review_digest
            and verify_manifest(manifest, staging_dir)
        )
        if current:
            return
        data: dict[str, object] = {
            "target_agent_did": manifest.target_agent_did,
        }
        promoted_paths = row.get("promoted_paths")
        if isinstance(promoted_paths, list):
            data["promoted_paths"] = promoted_paths
        self._ledger.set(manifest.import_id, CapabilityImportStatus.MODIFIED, **data)
        raise ValueError("capability import manifest changed after review")

    def _manifest_from_staging(self, staging_dir: Path) -> CapabilityImportManifest:
        staging = Path(staging_dir)
        path = staging / "import.json"
        if staging.is_symlink() or path.is_symlink() or not path.is_file():
            raise ValueError("capability import manifest is invalid")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return CapabilityImportManifest(
                import_id=payload["import_id"],
                target_agent_did=payload["target_agent_did"],
                archive_sha256=payload["archive_sha256"],
                files=tuple(CapabilityImportFile(**item) for item in payload["files"]),
                tools=tuple(payload["tools"]),
                skills=tuple(payload["skills"]),
                findings=tuple(payload["findings"]),
                limits=CapabilityImportLimits(**payload["limits"]),
                supplier_metadata=payload["supplier_metadata"],
                supplier_sbom_sha256=payload["supplier_sbom_sha256"],
                review_digest=payload["review_digest"],
            )
        except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
            raise ValueError("capability import manifest is invalid") from exc

    @staticmethod
    def _review_matches(manifest: CapabilityImportManifest, staging_dir: Path) -> bool:
        return (
            verify_manifest(manifest, staging_dir)
            and review_digest(manifest) == manifest.review_digest
        )

    def _target_paths(self, manifest: CapabilityImportManifest, base: Path) -> list[Path]:
        return [
            base / _target_relative(item.path)
            for item in manifest.files
            if _is_capability(item.path)
        ]

    def _stage_signed_files(
        self,
        manifest: CapabilityImportManifest,
        staging_dir: Path,
        temporary: Path,
        signer: Signer,
        operator_did: str,
    ) -> None:
        for item in manifest.files:
            if not _is_capability(item.path):
                continue
            source = Path(staging_dir) / item.path
            _reject_symlinked_parents(source, Path(staging_dir))
            if source.is_symlink() or not source.is_file():
                raise ValueError("capability import source is unavailable")
            target = temporary / _target_relative(item.path)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(source, flags)
            try:
                with os.fdopen(descriptor, "rb") as stream:
                    descriptor = -1
                    content = stream.read()
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
            if len(content) != item.size or hashlib.sha256(content).hexdigest() != item.sha256:
                raise ValueError("capability import source changed during promotion")
            target.write_bytes(content)
            target.chmod(0o600)
            content.decode("utf-8")
            write_signature_with_signer(
                target,
                content,
                signer_did=operator_did,
                signer=signer,
            )

    def _emit(
        self,
        import_id: str,
        action: str,
        actor_did: str,
        payload_hash: str | None,
        sink: AuditSink | None,
    ) -> None:
        target = sink or self._audit_sink
        if target is None:
            return
        emit(
            AuditEvent(
                actor_did=actor_did,
                action=action,
                target=import_id,
                outcome=action.rsplit(".", 1)[-1],
                payload_hash=payload_hash,
            ),
            target,
        )


def _is_capability(path: str) -> bool:
    return path.startswith("tools/") or path.startswith("skills/")


def _is_safe_review_path(path: str) -> bool:
    parts = path.split("/")
    return bool(path) and "\\" not in path and all(part not in {"", ".", ".."} for part in parts)


def _target_relative(path: str) -> Path:
    relative = Path(path)
    if relative.parts[0] == "tools":
        return Path(relative.name)
    return Path(*relative.parts[0:])


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _reject_symlinked_parents(path: Path, root: Path) -> None:
    """Keep promotion writes inside the configured capability root."""
    try:
        parts = path.relative_to(root).parts[:-1]
    except ValueError as exc:
        raise ValueError("capability target is outside root") from exc
    current = root
    for part in parts:
        current /= part
        if current.is_symlink():
            raise ValueError("capability target parent may not be a symlink")


def _with_sidecar(path: Path) -> tuple[Path, Path]:
    return path, path.with_name(path.name + ".arcsig")


def _snapshot(path: Path) -> tuple[Path, bytes | None, int | None]:
    if not path.exists():
        return path, None, None
    return path, path.read_bytes(), stat.S_IMODE(path.stat().st_mode)


def _restore(path: Path, content: bytes | None, mode: int | None) -> None:
    if content is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(content)
    if mode is not None:
        path.chmod(mode)


def _remove_empty_parents(path: Path, root: Path) -> None:
    parent = path.parent
    while parent != root and _is_under(parent, root):
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent
