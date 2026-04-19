"""arcskill.hub.installer -- End-to-end install pipeline.

Pipeline stages (SDD §3.8 Federal Install Pipeline):

    quarantine
        → verify_signature   (Sigstore / cosign)
        → check_crl          (embedded in verify stage)
        → scan               (regex + AST + semgrep + bandit)
        → dry_run            (Firecracker / Docker sandbox)
        → activate           (move quarantine → skills dir)
        → write_lock_file    (HubLockFile entry)
        → audit              (OTel + structured log)

Each stage is a pure function that raises on failure.  The orchestrator
calls them in sequence and cleans up quarantine on any exception.

CLI gate
--------
``install_path = "cli_only"`` in config means only the CLI may call this
module.  No agent-driven install path exists (D-08).
"""

from __future__ import annotations

import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path

from arcskill.hub.config import HubConfig
from arcskill.hub.dry_run import DryRunResult, run_dry_run
from arcskill.hub.errors import (
    HubDisabled,
    ScanVerdictFailed,
    SignatureInvalid,
    SourceNotAllowed,
)
from arcskill.hub.scanner import ScanResult, scan
from arcskill.hub.sources import FetchResult, make_adapter
from arcskill.hub.verify import VerifyResult, verify_bundle
from arcskill.lock import HubLockFile, SkillLockEntry

logger = logging.getLogger(__name__)

# Default base installation directory.
_DEFAULT_INSTALL_BASE = Path.home() / ".arc" / "skills"


# ---------------------------------------------------------------------------
# InstallResult
# ---------------------------------------------------------------------------


class InstallResult:
    """Records the outcome of a complete install pipeline run.

    Attributes
    ----------
    name:
        Canonical skill name.
    success:
        True if all stages passed and the skill is now active.
    fetch:
        FetchResult from the download stage.
    verify:
        VerifyResult from the signature/CRL stage.
    scan:
        ScanResult from the security-scanner stage.
    dry_run:
        DryRunResult from the sandbox stage.
    install_path:
        Path where the skill was installed (empty on failure).
    error:
        Error message if ``success=False``.
    """

    def __init__(
        self,
        name: str,
        *,
        success: bool,
        fetch: FetchResult | None = None,
        verify: VerifyResult | None = None,
        scan: ScanResult | None = None,
        dry_run: DryRunResult | None = None,
        install_path: Path | None = None,
        error: str = "",
    ) -> None:
        self.name = name
        self.success = success
        self.fetch = fetch
        self.verify = verify
        self.scan_result = scan
        self.dry_run = dry_run
        self.install_path = install_path
        self.error = error


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def install(
    name: str,
    source_name: str,
    config: HubConfig,
    *,
    install_base: Path | None = None,
    lock_path: Path | None = None,
    skip_sandbox: bool = False,
) -> InstallResult:
    """Run the full hub install pipeline for skill *name*.

    Parameters
    ----------
    name:
        Canonical skill name (e.g. ``"arc-official/summarise"``).
    source_name:
        Name of the ``SkillSource`` entry to use (must be in config.sources).
    config:
        Active hub configuration.
    install_base:
        Override the installation base directory.  Defaults to
        ``~/.arc/skills``.
    lock_path:
        Override the lock file path (used in tests).
    skip_sandbox:
        If True and tier is not federal, skip the dry-run sandbox.

    Returns
    -------
    InstallResult
        Full pipeline outcome.

    Raises
    ------
    HubDisabled
        If ``config.enabled`` is False.
    SourceNotAllowed
        If *source_name* is not in ``config.sources``.
    SignatureInvalid, CRLUnreachable, ScanVerdictFailed, SandboxRequired
        On respective stage failures.
    """
    _assert_hub_enabled(config)

    source = config.source_by_name(source_name)
    if source is None:
        raise SourceNotAllowed(
            f"Source {source_name!r} is not on the configured allowlist. "
            "Add it to [[skills.hub.sources]] in your config."
        )

    base = install_base or _DEFAULT_INSTALL_BASE
    quarantine_dir = base / ".hub" / "quarantine" / name.replace("/", "__")
    quarantine_dir.mkdir(parents=True, exist_ok=True)

    fetch_result: FetchResult | None = None
    verify_result: VerifyResult | None = None
    scan_result: ScanResult | None = None
    dry_run_result: DryRunResult | None = None

    try:
        # Stage 1: Download to quarantine.
        logger.info("[hub] Fetching skill %r from source %r", name, source_name)
        adapter = make_adapter(source)
        fetch_result = adapter.fetch(name, quarantine_dir)
        logger.info("[hub] Fetch complete: %s", fetch_result.local_path.name)

        # Stage 2: Signature verify + CRL check (combined in verify module).
        logger.info("[hub] Verifying signature for %r", name)
        verify_result = verify_bundle(
            bundle_path=fetch_result.local_path,
            source=source,
            config=config,
            content_hash=fetch_result.content_hash,
        )
        if verify_result.revoked:
            raise SignatureInvalid(
                f"Skill {name!r} hash {fetch_result.content_hash[:12]}… "
                "is in the CRL.  Install refused."
            )
        logger.info(
            "[hub] Signature verified: slsa_level=%d rekor_uuid=%s",
            verify_result.slsa_level,
            verify_result.rekor_uuid or "(none)",
        )

        # Stage 3: Security scan.
        logger.info("[hub] Scanning %r for security issues", name)
        scan_result = scan(fetch_result.local_path, config)
        logger.info(
            "[hub] Scan verdict=%r findings=%d",
            scan_result.verdict,
            len(scan_result.findings),
        )
        if scan_result.verdict == "dangerous" and config.policy.require_scan_pass:
            raise ScanVerdictFailed(
                verdict=scan_result.verdict,
                findings=[
                    f.message
                    for f in scan_result.findings
                    if f.severity in ("critical", "high")
                ][:10],
            )

        # Stage 4: Sandboxed dry-run.
        logger.info("[hub] Running sandboxed dry-run for %r", name)
        dry_run_result = run_dry_run(
            fetch_result.local_path,
            config,
            skip_sandbox=skip_sandbox,
        )
        if not dry_run_result.passed:
            raise RuntimeError(
                f"Dry-run failed for skill {name!r} "
                f"(exit_code={dry_run_result.exit_code}, "
                f"backend={dry_run_result.backend_used!r}). "
                f"Output: {dry_run_result.stdout[:200]!r}"
            )
        logger.info("[hub] Dry-run passed (backend=%r)", dry_run_result.backend_used)

        # Stage 5: Activate -- move quarantine → skills dir.
        install_path = _activate(
            name=name,
            bundle_path=fetch_result.local_path,
            base=base,
        )
        logger.info("[hub] Skill %r installed to %s", name, install_path)

        # Stage 6: Write HubLockFile entry.
        entry = SkillLockEntry(
            content_hash=fetch_result.content_hash,
            rekor_uuid=verify_result.rekor_uuid,
            slsa_level=verify_result.slsa_level,
            scan_verdict=scan_result.verdict,
            install_path=str(install_path),
            files=_list_installed_files(install_path),
            installed_at=datetime.now(UTC).isoformat(),
            updated_at=datetime.now(UTC).isoformat(),
        )
        lock = HubLockFile.load(lock_path)
        lock.add_or_update(name, entry)
        lock.save(lock_path)

        # Stage 7: Audit event.
        _emit_audit(
            name=name,
            source_name=source_name,
            verify=verify_result,
            scan=scan_result,
            dry_run=dry_run_result,
            install_path=install_path,
        )

        return InstallResult(
            name=name,
            success=True,
            fetch=fetch_result,
            verify=verify_result,
            scan=scan_result,
            dry_run=dry_run_result,
            install_path=install_path,
        )

    except Exception as exc:
        logger.error("[hub] Install failed for %r: %s", name, exc)
        _cleanup_quarantine(quarantine_dir)
        return InstallResult(
            name=name,
            success=False,
            fetch=fetch_result,
            verify=verify_result,
            scan=scan_result,
            dry_run=dry_run_result,
            error=str(exc),
        )


def uninstall(
    name: str,
    config: HubConfig,
    *,
    install_base: Path | None = None,
    lock_path: Path | None = None,
) -> bool:
    """Remove an installed skill.

    Returns True if the skill was found and removed, False otherwise.
    """
    _assert_hub_enabled(config)

    base = install_base or _DEFAULT_INSTALL_BASE
    install_path = base / name.replace("/", "__")

    if install_path.exists():
        shutil.rmtree(install_path)
        logger.info("[hub] Removed skill directory: %s", install_path)

    lock = HubLockFile.load(lock_path)
    removed = lock.remove(name)
    if removed:
        lock.save(lock_path)

    return removed


def update(
    name: str,
    source_name: str,
    config: HubConfig,
    *,
    install_base: Path | None = None,
    lock_path: Path | None = None,
    skip_sandbox: bool = False,
) -> InstallResult:
    """Re-run the full pipeline to update an installed skill.

    If the content hash has not changed, a no-op ``InstallResult`` is
    returned with ``success=True`` and a note in the error field.
    """
    _assert_hub_enabled(config)

    lock = HubLockFile.load(lock_path)
    existing = lock.skills.get(name)

    # Always re-run the full pipeline.  Hash change triggers re-scan.
    result = install(
        name=name,
        source_name=source_name,
        config=config,
        install_base=install_base,
        lock_path=lock_path,
        skip_sandbox=skip_sandbox,
    )

    if result.success and existing and result.fetch:
        if existing.content_hash == result.fetch.content_hash:
            logger.info("[hub] Skill %r is already up-to-date", name)
            result.error = "already_up_to_date"

    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _assert_hub_enabled(config: HubConfig) -> None:
    """Raise HubDisabled if hub is not enabled."""
    if not config.enabled:
        raise HubDisabled(
            "Skills Hub is disabled. Set [skills.hub] enabled = true in config."
        )


def _activate(name: str, bundle_path: Path, base: Path) -> Path:
    """Extract and move the bundle from quarantine to the skills directory.

    Returns the install path.
    """
    import tarfile

    safe_name = name.replace("/", "__")
    install_path = base / safe_name

    # Remove previous installation if present.
    if install_path.exists():
        shutil.rmtree(install_path)
    install_path.mkdir(parents=True)

    with tarfile.open(bundle_path) as tf:
        for member in tf.getmembers():
            if member.name.startswith("/") or ".." in member.name:
                logger.warning("Skipping unsafe tarball member: %r", member.name)
                continue
            tf.extract(member, path=install_path)

    return install_path


def _list_installed_files(install_path: Path) -> list[str]:
    """Return relative file paths under install_path."""
    return [
        str(p.relative_to(install_path))
        for p in sorted(install_path.rglob("*"))
        if p.is_file()
    ]


def _cleanup_quarantine(quarantine_dir: Path) -> None:
    """Remove the quarantine directory on install failure."""
    try:
        if quarantine_dir.exists():
            shutil.rmtree(quarantine_dir)
    except OSError as exc:
        logger.warning("Failed to clean up quarantine dir %s: %s", quarantine_dir, exc)


def _emit_audit(
    name: str,
    source_name: str,
    verify: VerifyResult,
    scan: ScanResult,
    dry_run: DryRunResult,
    install_path: Path,
) -> None:
    """Emit structured audit log entry for the install.

    In a full deployment this would emit an OpenTelemetry span.  Here we
    emit a structured log record that an OTel processor can pick up.
    """
    logger.info(
        "skills_hub.install_completed name=%r source=%r "
        "content_hash=%s rekor_uuid=%s slsa_level=%d "
        "scan_verdict=%r dry_run_backend=%r install_path=%s",
        name,
        source_name,
        verify.content_hash[:12] + "…",
        verify.rekor_uuid or "(none)",
        verify.slsa_level,
        scan.verdict,
        dry_run.backend_used,
        install_path,
    )
