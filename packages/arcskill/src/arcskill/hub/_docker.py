"""Docker container lifecycle for arcskill dry-run (non-federal fallback).

Sibling of ``arcskill.hub.dry_run``. The orchestrator there picks Docker
as the second-choice sandbox when Firecracker is unavailable and the tier
isn't federal; this module owns *how*: availability detection plus the
``_run_docker`` helper that drives ``arcrun.backends.docker.DockerBackend``.

Re-exported through ``arcskill.hub.dry_run`` — tests and callers continue
to do ``from arcskill.hub.dry_run import _docker_available, _run_docker``.
"""

from __future__ import annotations

import asyncio
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


async def _run_docker(fixture_cmd: str, skill_dir: Path) -> DryRunResult:
    """Execute the fixture inside a Docker container via DockerBackend."""
    backend_cls = _DockerBackend
    if backend_cls is None:
        logger.warning("arcrun.backends.docker not available (arcrun not installed)")
        return DryRunResult(passed=True, skipped=True, backend_used="skipped")

    backend = backend_cls(
        image="python:3.11-slim",
        network="none",
        pids_limit=32,
    )
    stdout_chunks: list[str] = []
    exit_code: int | None = None
    start = time.monotonic()

    try:
        handle = await asyncio.wait_for(
            backend.run(
                fixture_cmd,
                cwd="/skill",
                env={"PYTHONPATH": "/skill"},
                timeout=float(_DRY_RUN_TIMEOUT_SECONDS),
            ),
            timeout=_DRY_RUN_TIMEOUT_SECONDS + 2.0,
        )

        async for chunk in backend.stream(handle):
            stdout_chunks.append(chunk.decode("utf-8", errors="replace"))
            if sum(len(c) for c in stdout_chunks) > 4096:
                break

        exit_code = 0  # stream completion implies success
    except TimeoutError:
        logger.warning("Skill dry-run timed out after %ds", _DRY_RUN_TIMEOUT_SECONDS)
        exit_code = -1
    finally:
        await backend.close()

    duration = time.monotonic() - start
    stdout = "".join(stdout_chunks)[:4096]
    passed = exit_code == 0
    return DryRunResult(
        passed=passed,
        stdout=stdout,
        exit_code=exit_code,
        backend_used="docker",
        duration_s=duration,
    )
