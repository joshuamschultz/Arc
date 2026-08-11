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
import subprocess
import time
from functools import lru_cache
from pathlib import Path

from arcskill.hub._result import DryRunResult

logger = logging.getLogger(__name__)


# DockerBackend is an optional backend-extension dependency (requires arcrun).
# ``arcrun.backends`` is the intentionally public extension surface; this is
# the narrow deep-import exception to the root-facade rule, not an internal
# implementation-module import.
# Imported at module level so tests can patch arcskill.hub._docker._DockerBackend
# (and via re-export, arcskill.hub.dry_run._DockerBackend). Falls back to None
# when arcrun is not installed; _run_docker handles the None case.
try:
    from arcrun.backends import DockerBackend as _DockerBackend
except ImportError:
    _DockerBackend = None  # type: ignore[assignment,misc]  # reason: optional import — _run_docker checks for None and returns a skipped result when arcrun isn't installed


# Explicit re-export surface: dry_run imports these three names from here.
# _DockerBackend is an aliased import, so it needs listing for strict
# no-implicit-reexport to allow the re-export through dry_run.
__all__ = ["_DockerBackend", "_docker_available", "_run_docker"]


_DRY_RUN_TIMEOUT_SECONDS = 10

#: How long to wait for the daemon to answer before calling it absent. Short: an
#: unresponsive daemon is an unusable sandbox either way.
_DAEMON_PROBE_TIMEOUT_SECONDS = 5


@lru_cache(maxsize=1)
def _docker_available() -> bool:
    """True when a Docker daemon will actually run a container for us.

    The CLI on ``$PATH`` is not the question. A machine with the client installed
    and the daemon stopped answers every ``shutil.which`` truthfully and every
    ``docker run`` with a connection refusal — so a caller asking "do I have a
    sandbox?" would be told yes and then fail at the point where the sandbox was
    supposed to contain something. Ask the daemon.

    Cached: the answer gates a supply-chain decision taken many times per install,
    and a daemon started midway through one is not a state worth re-probing for.
    """
    binary = shutil.which("docker")
    if binary is None:
        return False
    try:
        probe = subprocess.run(  # noqa: S603 — resolved absolute path, fixed argv, no shell
            [binary, "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            timeout=_DAEMON_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


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
