"""Deterministic manifest and CycloneDX evidence for staged imports."""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path
from typing import Any

from arctrust import canonical_json

from arcagent.capabilities.skill_validator import validate_skill_folder
from arcagent.modules.capability_import.errors import CapabilityImportLayoutError
from arcagent.modules.capability_import.models import (
    CapabilityImportFile,
    CapabilityImportLimits,
    CapabilityImportManifest,
)
from arcagent.tools._dynamic_loader import AstValidator


def build_manifest(
    staging_dir: Path,
    *,
    import_id: str,
    target_agent_did: str,
    archive_sha256: str,
    limits: CapabilityImportLimits,
) -> CapabilityImportManifest:
    """Validate staged bytes without execution and return their review manifest."""
    files = _files(staging_dir)
    tools = _validate_tools(staging_dir)
    skills = _validate_skills(staging_dir)
    metadata = _supplier_metadata(staging_dir)
    supplier_sbom = _supplier_sbom_digest(staging_dir)
    payload = _payload(
        import_id,
        target_agent_did,
        archive_sha256,
        files,
        tools,
        skills,
        (),
        limits,
        metadata,
        supplier_sbom,
    )
    review_digest = hashlib.sha256(canonical_json(payload)).hexdigest()
    return CapabilityImportManifest(
        import_id=import_id,
        target_agent_did=target_agent_did,
        archive_sha256=archive_sha256,
        files=files,
        tools=tools,
        skills=skills,
        findings=(),
        limits=limits,
        supplier_metadata=metadata,
        supplier_sbom_sha256=supplier_sbom,
        review_digest=review_digest,
    )


def verify_manifest(manifest: CapabilityImportManifest, staging_dir: Path) -> bool:
    """Return whether every staged byte still matches its reviewed manifest."""
    try:
        current = _files(staging_dir)
    except (OSError, CapabilityImportLayoutError):
        return False
    return current == manifest.files


def write_evidence(staging_dir: Path, manifest: CapabilityImportManifest) -> tuple[Path, Path]:
    """Write canonical manifest and generated CycloneDX file BOM atomically."""
    manifest_path = staging_dir / "import.json"
    bom_path = staging_dir / "capability.bom.cdx.json"
    _atomic_json(manifest_path, manifest_dict(manifest))
    _atomic_json(bom_path, cyclonedx_bom(manifest))
    return manifest_path, bom_path


def manifest_dict(manifest: CapabilityImportManifest) -> dict[str, Any]:
    """Return the stable serializable form used for review and signatures."""
    return _payload(
        manifest.import_id,
        manifest.target_agent_did,
        manifest.archive_sha256,
        manifest.files,
        manifest.tools,
        manifest.skills,
        manifest.findings,
        manifest.limits,
        manifest.supplier_metadata,
        manifest.supplier_sbom_sha256,
    ) | {"review_digest": manifest.review_digest}


def review_digest(manifest: CapabilityImportManifest) -> str:
    """Recompute the digest over reviewed metadata, excluding the digest field."""
    payload = manifest_dict(manifest)
    payload.pop("review_digest", None)
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def cyclonedx_bom(manifest: CapabilityImportManifest) -> dict[str, Any]:
    """Generate a minimal CycloneDX BOM that inventories every staged file."""
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {"component": {"type": "application", "name": manifest.import_id}},
        "components": [
            {
                "type": "file",
                "name": item.path,
                "hashes": [{"alg": "SHA-256", "content": item.sha256}],
                "properties": [{"name": "arc:file-size", "value": str(item.size)}],
            }
            for item in manifest.files
        ],
    }


def _files(staging_dir: Path) -> tuple[CapabilityImportFile, ...]:
    excluded = {"import.json", "capability.bom.cdx.json"}
    entries: list[CapabilityImportFile] = []
    for path in sorted(staging_dir.rglob("*")):
        try:
            mode = path.lstat().st_mode
        except OSError as exc:
            raise CapabilityImportLayoutError("staged capability tree is unreadable") from exc
        if stat.S_ISDIR(mode):
            continue
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise CapabilityImportLayoutError(
                "staged capability tree contains a non-regular entry"
            )
        if path.name in excluded:
            continue
        entries.append(
            CapabilityImportFile(
                path.relative_to(staging_dir).as_posix(), _hash(path), path.stat().st_size
            )
        )
    return tuple(entries)


def _validate_tools(staging_dir: Path) -> tuple[str, ...]:
    validator = AstValidator()
    names: list[str] = []
    root = staging_dir / "tools"
    paths = sorted(root.glob("*.py")) if root.is_dir() else []
    for path in paths:
        try:
            validator.validate(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise CapabilityImportLayoutError("tool failed static validation") from exc
        names.append(path.stem)
    return tuple(names)


def _validate_skills(staging_dir: Path) -> tuple[str, ...]:
    root = staging_dir / "skills"
    names: list[str] = []
    for folder in sorted(root.iterdir()) if root.is_dir() else []:
        result = validate_skill_folder(folder, "import")
        if not result.ok or result.entry is None:
            raise CapabilityImportLayoutError("skill failed validation")
        names.append(result.entry.name)
    return tuple(names)


def _supplier_metadata(staging_dir: Path) -> dict[str, Any]:
    path = staging_dir / "capability-import.toml"
    if not path.is_file():
        return {}
    import tomllib

    try:
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise CapabilityImportLayoutError("invalid supplier metadata") from exc
    return parsed if isinstance(parsed, dict) else {}


def _supplier_sbom_digest(staging_dir: Path) -> str | None:
    path = staging_dir / "sbom.cdx.json"
    return _hash(path) if path.is_file() else None


def _payload(
    import_id: str,
    target_agent_did: str,
    archive_sha256: str,
    files: tuple[CapabilityImportFile, ...],
    tools: tuple[str, ...],
    skills: tuple[str, ...],
    findings: tuple[str, ...],
    limits: CapabilityImportLimits,
    supplier_metadata: dict[str, Any],
    supplier_sbom_sha256: str | None,
) -> dict[str, Any]:
    return {
        "archive_sha256": archive_sha256,
        "files": [item.__dict__ for item in files],
        "findings": list(findings),
        "import_id": import_id,
        "limits": limits.__dict__,
        "skills": list(skills),
        "supplier_metadata": supplier_metadata,
        "supplier_sbom_sha256": supplier_sbom_sha256,
        "target_agent_did": target_agent_did,
        "tools": list(tools),
    }


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(canonical_json(data))
    temporary.chmod(0o600)
    temporary.replace(path)


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
