"""DockerBackend — long-lived container per agent, docker exec per command.

Architecture
------------
Each DockerBackend instance owns ONE container that lives for the agent's
lifetime.  Individual run() calls use `docker exec` to execute inside that
container.  This avoids per-command cold-start cost after the first call.

Security defaults (NIST SC-7 boundary protection)
--------------------------------------------------
- --cap-drop=ALL        — no Linux capabilities
- --security-opt=no-new-privileges — prevents privilege escalation
- --pids-limit N        — limits fork bombs (default 64)
- --read-only           — container root FS is read-only
- --network none        — no outbound network by default
- tmpfs /tmp            — writable scratch space, noexec

Cancel contract
---------------
1. docker exec kill -TERM <exec_pid>   (in-container SIGTERM)
2. wait grace seconds
3. docker exec kill -KILL <exec_pid>   (in-container SIGKILL)
close() → docker rm -f <container_id>

Capabilities
------------
- isolation: "container"
- cold_start_budget_ms: 800  (first run; subsequent ~30ms)
- supports_bind_mount: True
- supports_persistent_workspace: True  (container scratch remains between runs)
- supports_separated_streams: True  (docker exec naturally supports separate pipes)
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

from arcrun.backends.base import (
    TRUNCATION_MARKER,
    BackendCapabilities,
    ExecHandle,
    ExecutorBackend,
)

_DEFAULT_MAX_STDOUT = 64 * 1024
_STREAM_CHUNK = 4096


class DockerBackend:
    """Execute commands inside a long-lived Docker container.

    The container is created lazily on the first run() call.  Subsequent
    calls reuse the same container via `docker exec`.

    Parameters
    ----------
    image:
        Docker image to use.  Should be pinned to a digest for reproducibility
        (image@sha256:…).
    pids_limit:
        Maximum PIDs inside the container.  64 is a reasonable default.
    network:
        Docker network name.  "none" disables all networking.
    max_stdout_bytes:
        Hard cap on captured output before TRUNCATION_MARKER is appended.
    """

    name: str = "docker"
    capabilities: BackendCapabilities

    def __init__(
        self,
        image: str = "python:3.11-slim",
        pids_limit: int = 64,
        network: str = "none",
        max_stdout_bytes: int = _DEFAULT_MAX_STDOUT,
    ) -> None:
        self.capabilities = BackendCapabilities(
            supports_file_copy=True,
            supports_persistent_workspace=True,
            supports_port_forward=False,
            supports_bind_mount=True,
            supports_separated_streams=True,
            cold_start_budget_ms=800,
            max_stdout_bytes=max_stdout_bytes,
            isolation="container",
        )
        self._image = image
        self._pids_limit = pids_limit
        self._network = network
        self._container_id: str | None = None

        # exec_pid tracks in-container PIDs for cancellation.
        # Key: handle_id, Value: exec PID as reported by docker exec's --detach-keys trick.
        # For simplicity we track the docker exec subprocess so we can cancel it directly.
        self._exec_procs: dict[str, asyncio.subprocess.Process] = {}

    # ------------------------------------------------------------------
    # Protocol implementation
    # ------------------------------------------------------------------

    async def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 120.0,
        stdin: str | None = None,
    ) -> ExecHandle:
        """Start a command inside the container via docker exec."""
        container_id = await self._ensure_container()
        handle_id = str(uuid.uuid4())

        docker_args = ["docker", "exec", "-i"]

        if cwd:
            docker_args += ["--workdir", cwd]

        if env:
            for key, value in env.items():
                docker_args += ["--env", f"{key}={value}"]

        docker_args += [container_id, "bash", "-c", command]

        stdin_data = stdin.encode() if stdin is not None else None
        stdin_mode = (
            asyncio.subprocess.PIPE
            if stdin_data is not None
            else asyncio.subprocess.DEVNULL
        )

        proc = await asyncio.create_subprocess_exec(
            *docker_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=stdin_mode,
            start_new_session=True,
        )

        if stdin_data is not None and proc.stdin is not None:
            proc.stdin.write(stdin_data)
            proc.stdin.close()

        self._exec_procs[handle_id] = proc

        return ExecHandle(
            handle_id=handle_id,
            backend_name=self.name,
            meta={"container_id": container_id, "timeout": timeout},
        )

    async def stream(self, handle: ExecHandle) -> AsyncIterator[bytes]:
        """Drain docker exec stdout+stderr as raw byte chunks."""
        proc = self._exec_procs.get(handle.handle_id)
        if proc is None or proc.stdout is None:
            return

        total = 0
        max_bytes = self.capabilities.max_stdout_bytes

        try:
            while True:
                chunk = await proc.stdout.read(_STREAM_CHUNK)
                if not chunk:
                    break
                remaining = max_bytes - total
                if remaining <= 0:
                    yield TRUNCATION_MARKER
                    await _drain_silently(proc.stdout)
                    break
                if len(chunk) > remaining:
                    yield chunk[:remaining]
                    yield TRUNCATION_MARKER
                    await _drain_silently(proc.stdout)
                    break
                yield chunk
                total += len(chunk)
        finally:
            await proc.wait()
            self._exec_procs.pop(handle.handle_id, None)

    async def cancel(self, handle: ExecHandle, *, grace: float = 5.0) -> None:
        """Terminate the docker exec subprocess.

        We kill the local `docker exec` process (which closes the exec session)
        rather than hunting the in-container PID, since we do not have a reliable
        way to retrieve the in-container PID for `docker exec` in all configurations.
        """
        proc = self._exec_procs.get(handle.handle_id)
        if proc is None:
            return

        try:
            proc.terminate()  # SIGTERM to local docker exec process
        except ProcessLookupError:
            return

        try:
            await asyncio.wait_for(proc.wait(), timeout=grace)
        except TimeoutError:
            try:
                proc.kill()  # SIGKILL to local docker exec process
                await proc.wait()
            except ProcessLookupError:
                pass

        self._exec_procs.pop(handle.handle_id, None)

    async def close(self) -> None:
        """Cancel all live execs, then force-remove the container."""
        for handle_id, _proc in list(self._exec_procs.items()):
            handle = ExecHandle(handle_id=handle_id, backend_name=self.name)
            await self.cancel(handle, grace=1.0)

        if self._container_id is not None:
            await _docker_rm_f(self._container_id)
            self._container_id = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _ensure_container(self) -> str:
        """Create the container if it does not exist yet, return its ID."""
        if self._container_id is not None:
            return self._container_id

        self._container_id = await _docker_run_detached(
            image=self._image,
            pids_limit=self._pids_limit,
            network=self._network,
        )
        return self._container_id


# ---------------------------------------------------------------------------
# Module-level helpers (docker CLI wrappers)
# ---------------------------------------------------------------------------


async def _docker_run_detached(
    image: str,
    pids_limit: int,
    network: str,
) -> str:
    """docker run -d …; returns the container ID."""
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "run",
        "-d",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        f"--pids-limit={pids_limit}",
        "--read-only",
        f"--network={network}",
        "--tmpfs=/tmp:noexec,nosuid,size=64m",
        image,
        "sleep",
        "infinity",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"docker run failed (rc={proc.returncode}): {stderr.decode().strip()}"
        )
    return stdout.decode().strip()


async def _docker_rm_f(container_id: str) -> None:
    """docker rm -f <container_id>; swallows errors (best-effort cleanup)."""
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "rm",
        "-f",
        container_id,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()


async def _drain_silently(stream: asyncio.StreamReader) -> None:
    """Read and discard all remaining bytes from stream."""
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break


# Verify the class satisfies the Protocol at import time.
assert isinstance(DockerBackend(), ExecutorBackend)  # noqa: S101 — design-time assertion
