"""arcskill.hub.installer -- End-to-end install pipeline.

Pipeline stages (SDD §3.8 Federal Install Pipeline):

    quarantine
        → _stage_fetch        (download to quarantine)
        → _stage_verify_signature  (Sigstore / cosign + CRL check)
        → _stage_crl_check    (revocation check — performed inside verify)
        → _stage_scan         (regex + AST + semgrep + bandit)
        → _stage_dry_run      (Firecracker / Docker sandbox)
        → _stage_activate     (move quarantine → skills dir)
        → _stage_lock         (write HubLockFile entry)
        → _stage_audit        (OTel + structured log)

The top-level ``install()`` function is a thin pipeline orchestrator that
calls each stage in sequence and cleans up quarantine on any exception.
Each stage receives and returns (or mutates) an ``InstallContext`` Pydantic
model that accumulates state across stages.

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

from pydantic import BaseModel, ConfigDict

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
# InstallContext — pipeline state accumulator
# ---------------------------------------------------------------------------


class InstallContext(BaseModel):
    """Mutable state passed through each install pipeline stage.

    Attributes
    ----------
    name:
        Canonical skill name.
    source_name:
        Name of the SkillSource used.
    config:
        Active hub configuration.
    install_base:
        Installation base directory.
    lock_path:
        Override path for the lock file (used in tests).
    skip_sandbox:
        If True and tier is not federal, skip the dry-run sandbox.
    quarantine_dir:
        Path to the quarantine directory for this install.
    fetch:
        FetchResult from _stage_fetch (None until that stage runs).
    verify:
        VerifyResult from _stage_verify_signature (None until that stage runs).
    scan:
        ScanResult from _stage_scan (None until that stage runs).
    dry_run:
        DryRunResult from _stage_dry_run (None until that stage runs).
    install_path:
        Final install path set by _stage_activate (None until activated).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    source_name: str
    config: HubConfig
    install_base: Path
    lock_path: Path | None
    skip_sandbox: bool
    quarantine_dir: Path
    fetch: FetchResult | None = None
    verify: VerifyResult | None = None
    scan: ScanResult | None = None
    dry_run: DryRunResult | None = None
    install_path: Path | None = None


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

    ctx = InstallContext(
        name=name,
        source_name=source_name,
        config=config,
        install_base=base,
        lock_path=lock_path,
        skip_sandbox=skip_sandbox,
        quarantine_dir=quarantine_dir,
    )

    try:
        _stage_fetch(ctx)
        _stage_verify_signature(ctx)
        _stage_crl_check(ctx)
        _stage_scan(ctx)
        _stage_dry_run(ctx)
        _stage_activate(ctx)
        _stage_lock(ctx)
        _stage_audit(ctx)

        return InstallResult(
            name=name,
            success=True,
            fetch=ctx.fetch,
            verify=ctx.verify,
            scan=ctx.scan,
            dry_run=ctx.dry_run,
            install_path=ctx.install_path,
        )

    except Exception as exc:
        logger.error("[hub] Install failed for %r: %s", name, exc)
        _cleanup_quarantine(quarantine_dir)
        return InstallResult(
            name=name,
            success=False,
            fetch=ctx.fetch,
            verify=ctx.verify,
            scan=ctx.scan,
            dry_run=ctx.dry_run,
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
# Pipeline stages (each ≤ 25 LOC, receives InstallContext)
# ---------------------------------------------------------------------------


def _stage_fetch(ctx: InstallContext) -> None:
    """Stage 1: Download the skill bundle into the quarantine directory.

    Args:
        ctx: Mutable install context.  Sets ctx.fetch on success.
    """
    logger.info("[hub] Fetching skill %r from source %r", ctx.name, ctx.source_name)
    source = ctx.config.source_by_name(ctx.source_name)
    adapter = make_adapter(source)  # type: ignore[arg-type]
    ctx.fetch = adapter.fetch(ctx.name, ctx.quarantine_dir)
    logger.info("[hub] Fetch complete: %s", ctx.fetch.local_path.name)


def _stage_verify_signature(ctx: InstallContext) -> None:
    """Stage 2: Verify the Sigstore / cosign signature bundle.

    Args:
        ctx: Mutable install context.  Sets ctx.verify on success.

    Raises:
        SignatureInvalid: If the signature is invalid.
    """
    if ctx.fetch is None:
        raise RuntimeError("_stage_verify_signature called before _stage_fetch")
    logger.info("[hub] Verifying signature for %r", ctx.name)
    source = ctx.config.source_by_name(ctx.source_name)
    ctx.verify = verify_bundle(
        bundle_path=ctx.fetch.local_path,
        source=source,  # type: ignore[arg-type]
        config=ctx.config,
        content_hash=ctx.fetch.content_hash,
    )
    logger.info(
        "[hub] Signature verified: slsa_level=%d rekor_uuid=%s",
        ctx.verify.slsa_level,
        ctx.verify.rekor_uuid or "(none)",
    )


def _stage_crl_check(ctx: InstallContext) -> None:
    """Stage 3: Check whether the bundle hash appears in the CRL.

    Args:
        ctx: Mutable install context.

    Raises:
        SignatureInvalid: If the bundle hash is in the revocation list.
    """
    if ctx.verify is None or ctx.fetch is None:
        raise RuntimeError("_stage_crl_check called before _stage_verify_signature")
    if ctx.verify.revoked:
        raise SignatureInvalid(
            f"Skill {ctx.name!r} hash {ctx.fetch.content_hash[:12]}… "
            "is in the CRL.  Install refused."
        )


def _stage_scan(ctx: InstallContext) -> None:
    """Stage 4: Run the security scanner against the bundle.

    Args:
        ctx: Mutable install context.  Sets ctx.scan on success.

    Raises:
        ScanVerdictFailed: If the verdict is dangerous and policy requires pass.
    """
    if ctx.fetch is None:
        raise RuntimeError("_stage_scan called before _stage_fetch")
    logger.info("[hub] Scanning %r for security issues", ctx.name)
    ctx.scan = scan(ctx.fetch.local_path, ctx.config)
    logger.info(
        "[hub] Scan verdict=%r findings=%d",
        ctx.scan.verdict,
        len(ctx.scan.findings),
    )
    if ctx.scan.verdict == "dangerous" and ctx.config.policy.require_scan_pass:
        raise ScanVerdictFailed(
            verdict=ctx.scan.verdict,
            findings=[f.message for f in ctx.scan.findings if f.severity in ("critical", "high")][
                :10
            ],
        )


def _stage_dry_run(ctx: InstallContext) -> None:
    """Stage 5: Execute the skill in a sandboxed environment.

    Args:
        ctx: Mutable install context.  Sets ctx.dry_run on success.

    Raises:
        RuntimeError: If the dry-run fails (non-zero exit or backend error).
    """
    if ctx.fetch is None:
        raise RuntimeError("_stage_dry_run called before _stage_fetch")
    logger.info("[hub] Running sandboxed dry-run for %r", ctx.name)
    ctx.dry_run = run_dry_run(
        ctx.fetch.local_path,
        ctx.config,
        skip_sandbox=ctx.skip_sandbox,
    )
    if not ctx.dry_run.passed:
        raise RuntimeError(
            f"Dry-run failed for skill {ctx.name!r} "
            f"(exit_code={ctx.dry_run.exit_code}, "
            f"backend={ctx.dry_run.backend_used!r}). "
            f"Output: {ctx.dry_run.stdout[:200]!r}"
        )
    logger.info("[hub] Dry-run passed (backend=%r)", ctx.dry_run.backend_used)


def _stage_activate(ctx: InstallContext) -> None:
    """Stage 6: Extract and move the bundle from quarantine to the skills dir.

    Args:
        ctx: Mutable install context.  Sets ctx.install_path on success.
    """
    if ctx.fetch is None:
        raise RuntimeError("_stage_activate called before _stage_fetch")
    ctx.install_path = _activate(
        name=ctx.name,
        bundle_path=ctx.fetch.local_path,
        base=ctx.install_base,
    )
    logger.info("[hub] Skill %r installed to %s", ctx.name, ctx.install_path)


def _stage_lock(ctx: InstallContext) -> None:
    """Stage 7: Write or update the HubLockFile entry for this skill.

    Args:
        ctx: Mutable install context.
    """
    if ctx.fetch is None or ctx.verify is None or ctx.scan is None or ctx.install_path is None:
        raise RuntimeError("_stage_lock called before pipeline stages completed")
    entry = SkillLockEntry(
        content_hash=ctx.fetch.content_hash,
        rekor_uuid=ctx.verify.rekor_uuid,
        slsa_level=ctx.verify.slsa_level,
        scan_verdict=ctx.scan.verdict,
        install_path=str(ctx.install_path),
        files=_list_installed_files(ctx.install_path),
        installed_at=datetime.now(UTC).isoformat(),
        updated_at=datetime.now(UTC).isoformat(),
    )
    lock = HubLockFile.load(ctx.lock_path)
    lock.add_or_update(ctx.name, entry)
    lock.save(ctx.lock_path)


def _stage_audit(ctx: InstallContext) -> None:
    """Stage 8: Emit a structured audit log entry for the completed install.

    Args:
        ctx: Mutable install context (all stage results must be set).
    """
    if (
        ctx.fetch is None
        or ctx.verify is None
        or ctx.scan is None
        or ctx.dry_run is None
        or ctx.install_path is None
    ):
        raise RuntimeError("_stage_audit called before pipeline stages completed")
    _emit_audit(
        name=ctx.name,
        source_name=ctx.source_name,
        verify=ctx.verify,
        scan=ctx.scan,
        dry_run=ctx.dry_run,
        install_path=ctx.install_path,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _assert_hub_enabled(config: HubConfig) -> None:
    """Raise HubDisabled if hub is not enabled."""
    if not config.enabled:
        raise HubDisabled("Skills Hub is disabled. Set [skills.hub] enabled = true in config.")


def _activate(name: str, bundle_path: Path, base: Path) -> Path:
    """Extract and move the bundle from quarantine to the skills directory.

    Returns the install path.
    """
    import tarfile

    safe_name = name.replace("/", "__")
    install_path = base / safe_name

    if install_path.exists():
        shutil.rmtree(install_path)
    install_path.mkdir(parents=True)

    with tarfile.open(bundle_path) as tf:
        for member in tf.getmembers():
            if member.name.startswith("/") or ".." in member.name:
                logger.warning("Skipping unsafe tarball member: %r", member.name)
                continue
            # filter="data" (PEP 706) blocks symlinks, special files, and
            # path traversal at the tarfile level. Required default in Py3.14+.
            tf.extract(member, path=install_path, filter="data")

    return install_path


def _list_installed_files(install_path: Path) -> list[str]:
    """Return relative file paths under install_path."""
    return [
        str(p.relative_to(install_path)) for p in sorted(install_path.rglob("*")) if p.is_file()
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
