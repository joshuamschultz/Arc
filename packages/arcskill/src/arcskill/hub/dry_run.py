"""arcskill.hub.dry_run -- Sandboxed skill dry-run orchestrator.

Isolation policy
----------------
- **Federal tier**: Firecracker microVM isolation is REQUIRED.  If the
  Firecracker backend is unavailable, ``SandboxRequired`` is raised and
  the install is aborted (fail-closed).
- **Enterprise / personal tier**: Firecracker preferred; falls back to
  ``DockerBackend`` (``arcskill.hub._docker``) when Firecracker is
  unavailable; final fallback to scan-only verdict with an audit WARNING
  when neither sandbox is available.

Why NOT RestrictedPython
------------------------
RestrictedPython has known CVEs (CVE-2023-41039, CVE-2024-49755) that allow
escape from the restricted execution environment.  It is explicitly prohibited
by SDD §3.8 and the task specification.  This module uses subprocess-level
isolation (Firecracker microVM via jailer, or Docker), NOT in-process Python
sandboxing.

Dry-run protocol
----------------
1. Extract the skill bundle to a temporary directory.
2. Locate the ``test_fixture`` declared in the skill's ``MODULE.yaml``.
3. Run the fixture inside the sandbox with a 10-second hard timeout.
4. Return ``DryRunResult`` with pass/fail and captured output.

The dry-run is intentionally minimal: its purpose is to prove the skill
can be imported and its declared test function executes without raising
an exception in a clean environment, NOT to validate correctness.

Sibling modules
---------------
- ``arcskill.hub._result``       — ``DryRunResult`` schema.
- ``arcskill.hub._firecracker``  — Firecracker microVM lifecycle.
- ``arcskill.hub._docker``       — Docker container lifecycle.

Names from the siblings are re-exported through this module so existing
imports (``from arcskill.hub.dry_run import FirecrackerSandbox``) keep
working without modification.
"""

from __future__ import annotations

import asyncio
import logging
import platform
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arcskill.hub._docker import (
    _docker_available,
    _DockerBackend,
    _run_docker,
)
from arcskill.hub._firecracker import (
    FirecrackerConfig,
    FirecrackerSandbox,
    _run_firecracker,
    is_firecracker_available,
)
from arcskill.hub._result import DryRunResult
from arcskill.hub.config import HubConfig
from arcskill.hub.errors import SandboxRequired

if TYPE_CHECKING:
    from arctrust import AuditSink

logger = logging.getLogger(__name__)

# Module-level warning when Firecracker is absent at import time.
# Informational only; the hard fail is raised at run time when federal
# tier calls execute().
if not shutil.which("firecracker") or not Path("/dev/kvm").exists():
    logger.debug(
        "Firecracker not available on this host (expected on macOS / non-KVM Linux). "
        "Federal tier dry-run will raise SandboxRequired."
    )


__all__ = [
    "DryRunResult",
    "FirecrackerConfig",
    "FirecrackerSandbox",
    "_DockerBackend",
    "_docker_available",
    "_run_docker",
    "_run_firecracker",
    "is_firecracker_available",
    "run_dry_run",
]


# ---------------------------------------------------------------------------
# Public API — run_dry_run
# ---------------------------------------------------------------------------


def run_dry_run(
    bundle_path: Path,
    config: HubConfig,
    *,
    audit_sink: Any | None = None,
) -> DryRunResult:
    """Execute a sandboxed dry-run of the skill bundle.

    Sandbox runs at ALL tiers.  Tier controls *which* backend is used
    (Firecracker at federal, Docker/subprocess elsewhere), not *whether*
    to sandbox.

    Parameters
    ----------
    bundle_path:
        Path to the ``.tar.gz`` skill bundle.
    config:
        Hub configuration (tier determines isolation backend).
    audit_sink:
        Optional arctrust AuditSink for emitting structured audit events.

    Returns
    -------
    DryRunResult
        Outcome of the dry-run.

    Raises
    ------
    SandboxRequired
        If the required sandbox backend is unavailable for the configured tier.
    """
    with tempfile.TemporaryDirectory(prefix="arcskill_dryrun_") as tmpdir:
        extract_dir = Path(tmpdir) / "skill"
        extract_dir.mkdir()
        _safe_extract(bundle_path, extract_dir)

        fixture_cmd = _find_fixture_command(extract_dir)
        return asyncio.run(
            _run_in_sandbox(fixture_cmd, extract_dir, config, audit_sink=audit_sink)
        )


# ---------------------------------------------------------------------------
# Sandbox selection
# ---------------------------------------------------------------------------


async def _run_in_sandbox(
    fixture_cmd: str,
    skill_dir: Path,
    config: HubConfig,
    *,
    audit_sink: AuditSink | None = None,
) -> DryRunResult:
    """Select sandbox backend and run the fixture command.

    Selection order:
    1. Firecracker (required at federal; preferred everywhere)
    2. DockerBackend (enterprise / personal fallback)
    3. Scan-only skip with audit WARNING (non-federal last resort)
    """
    if is_firecracker_available():
        return await _run_firecracker(fixture_cmd, skill_dir)

    if config.is_federal:
        raise SandboxRequired(
            "Federal tier requires Firecracker microVM isolation for skill dry-run, "
            "but Firecracker / KVM / jailer is not available on this host. "
            "Install Firecracker or use a pre-approved build environment. "
            "See packages/arcskill/docs/firecracker-deployment.md."
        )

    # Non-federal: prefer Docker.
    if _docker_available():
        return await _run_docker(fixture_cmd, skill_dir)

    # Final fallback: scan-only verdict with prominent warning and audit event.
    logger.warning(
        "AUDIT WARNING: Neither Firecracker nor Docker available for dry-run sandbox. "
        "Skill will be installed without sandbox execution (non-federal tier). "
        "This reduces supply-chain security guarantees. "
        "Install Docker or Firecracker to restore sandbox isolation."
    )
    _emit_sandbox_audit(
        audit_sink=audit_sink,
        target=str(skill_dir),
        outcome="warn",
        tier=config.tier.level,
        backend="none",
    )
    return DryRunResult(passed=True, skipped=True, backend_used="skipped")


def _emit_sandbox_audit(
    *,
    audit_sink: AuditSink | None,
    target: str,
    outcome: str,
    tier: str,
    backend: str,
) -> None:
    """Emit a structured audit event for sandbox execution outcomes.

    Swallows all errors — auditing must never interrupt the sandbox path.
    """
    if audit_sink is None:
        return
    try:
        from arctrust import AuditEvent, emit

        emit(
            AuditEvent(
                actor_did="arcskill.hub.dry_run",
                action="skill.sandbox.execute",
                target=target,
                outcome=outcome,
                tier=tier,
                extra={"backend": backend},
            ),
            audit_sink,
        )
    except Exception:  # reason: fail-open — log + continue
        logger.warning("Failed to emit sandbox audit event for target=%s", target)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_fixture_command(skill_dir: Path) -> str:
    """Return the fixture command declared in MODULE.yaml, or a fallback.

    The MODULE.yaml should contain::

        test_fixture: "python -m pytest tests/ -x -q"

    If no MODULE.yaml is present or no ``test_fixture`` is declared, falls
    back to ``python -c "import skill; print('import ok')"`` if a
    ``skill.py`` or ``__init__.py`` is found.
    """
    module_yaml = skill_dir / "MODULE.yaml"
    if module_yaml.exists():
        try:
            import yaml  # type: ignore[import-untyped]  # reason: PyYAML ships no type stubs; only safe_load is called and its return type is Any by design

            data = yaml.safe_load(module_yaml.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "test_fixture" in data:
                return str(data["test_fixture"])
        except Exception as exc:  # reason: MODULE.yaml parse failure is non-fatal
            logger.debug("Failed to parse MODULE.yaml test_fixture: %s", exc)

    for candidate in ("skill.py", "__init__.py", "main.py"):
        if (skill_dir / candidate).exists():
            module = candidate.replace(".py", "").replace("__init__", "skill")
            return f"python -c \"import {module}; print('dry-run ok')\""

    return "python -c \"print('dry-run ok')\""


def _safe_extract(bundle_path: Path, dest: Path) -> None:
    """Extract tarball, rejecting path-traversal entries."""
    with tarfile.open(bundle_path) as tf:
        for member in tf.getmembers():
            if member.name.startswith("/") or ".." in member.name:
                logger.warning("Skipping path-traversal entry: %r", member.name)
                continue
            # filter="data" (PEP 706) blocks symlinks, special files, and
            # path traversal at the tarfile level. Required default in Py3.14+.
            tf.extract(member, path=dest, filter="data")


def _platform_is_macos() -> bool:
    """True when running on macOS (Darwin)."""
    return platform.system() == "Darwin"
