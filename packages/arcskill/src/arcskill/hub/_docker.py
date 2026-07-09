"""Docker container lifecycle for arcskill dry-run (non-federal fallback).

Sibling of ``arcskill.hub.dry_run``. The orchestrator there picks Docker
as the second-choice sandbox when Firecracker is unavailable and the tier
isn't federal; this module owns *how*: availability detection plus the
``_run_docker`` helper that drives ``arcrun.backends.docker.DockerBackend``.

Re-exported through ``arcskill.hub.dry_run`` — tests and callers continue
to do ``from arcskill.hub.dry_run import _docker_available, _run_docker``.
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

from arcskill.hub._result import DryRunResult

logger = logging.getLogger(__name__)


# DockerBackend is an optional dependency (requires arcrun package).
# Imported at module level so tests can patch arcskill.hub._docker._DockerBackend
# (and via re-export, arcskill.hub.dry_run._DockerBackend). Falls back to None
# when arcrun is not installed; _run_docker handles the None case.
try:
    from arcrun.backends.docker import DockerBackend as _DockerBackend
except ImportError:
    _DockerBackend = None  # type: ignore[assignment,misc]  # reason: optional import — _run_docker checks for None and returns a skipped result when arcrun isn't installed


# Explicit re-export surface: dry_run imports these three names from here.
# _DockerBackend is an aliased import, so it needs listing for strict
# no-implicit-reexport to allow the re-export through dry_run.
__all__ = ["_DockerBackend", "_docker_available", "_run_docker"]


_DRY_RUN_TIMEOUT_SECONDS = 10


def _docker_available() -> bool:
    """True if the docker CLI is on $PATH."""
    return bool(shutil.which("docker"))


async def _run_docker(
    fixture_cmd: str,
    skill_dir: Path,
    *,
    mount: bool = False,
    timeout_s: int = _DRY_RUN_TIMEOUT_SECONDS,
) -> DryRunResult:
    """Execute the fixture inside a Docker container via DockerBackend.

    When ``mount`` is set the skill directory is bind-mounted read-write at
    ``/workspace`` (with the rest of the container FS read-only) and the command
    runs there — required for the golden-task eval runner, which must see the
    materialized bundle (SPEC-044 P3.3). The install dry-run keeps ``mount=False``
    (a smoke-import check that needs no bundle on disk).
    """
    backend_cls = _DockerBackend
    if backend_cls is None:
        logger.warning("arcrun.backends.docker not available (arcrun not installed)")
        return DryRunResult(passed=True, skipped=True, backend_used="skipped")

    workdir = "/workspace" if mount else "/skill"
    backend = backend_cls(
        image="python:3.11-slim",
        network="none",
        pids_limit=32,
        workspace_mount=skill_dir if mount else None,
    )
    start = time.monotonic()

    # run_separated returns the container's REAL exit code (and -1 on timeout), so a
    # failing dry-run fixture is never silently reported as passed. The streaming API
    # cannot observe the exit code, which is why it used to hardcode 0.
    try:
        result = await backend.run_separated(
            fixture_cmd,
            cwd=workdir,
            env={"PYTHONPATH": workdir, "PYTHONDONTWRITEBYTECODE": "1"},
            timeout=float(timeout_s),
        )
    finally:
        await backend.close()

    duration = time.monotonic() - start
    if result.exit_code == -1:
        logger.warning("Skill dry-run timed out after %ds", timeout_s)
    stdout = result.stdout.decode("utf-8", errors="replace")[:4096]
    return DryRunResult(
        passed=result.exit_code == 0,
        stdout=stdout,
        exit_code=result.exit_code,
        backend_used="docker",
        duration_s=duration,
    )
