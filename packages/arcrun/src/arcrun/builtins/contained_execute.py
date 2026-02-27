"""Container-isolated Python execution via Docker/Podman.

Security: The container runtime socket (Docker/Podman) grants host-level
container management privilege. Restrict socket file permissions to trusted
users only. See NIST SC-7 (boundary protection).
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import tarfile
import time
from pathlib import Path
from typing import Any

from arcrun.types import Tool, ToolContext

logger = logging.getLogger(__name__)

MAX_CODE_BYTES = 1_048_576  # 1 MiB


class SandboxError(Exception):
    """Base for all container sandbox errors."""


class SandboxUnavailableError(SandboxError):
    """No container runtime found."""


class SandboxTimeoutError(SandboxError):
    """Container exceeded timeout."""


class SandboxOOMError(SandboxError):
    """Container killed by OOM (exit code 137)."""


class SandboxRuntimeError(SandboxError):
    """Script execution failed."""


def _detect_socket() -> str:
    """Auto-detect container runtime socket."""
    candidates = [
        os.environ.get("DOCKER_HOST", ""),
        f"unix:///run/user/{os.getuid()}/podman/podman.sock",
        "unix:///var/run/docker.sock",
        "unix:///var/run/podman/podman.sock",
    ]
    for sock in candidates:
        if sock and Path(sock.replace("unix://", "")).exists():
            return sock
    raise SandboxUnavailableError("No container runtime socket found. Install Docker or Podman.")


def _inject_code_via_tar(container: Any, code: str) -> None:
    """Inject Python code into container's /tmp via tar stream."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        data = code.encode("utf-8")
        info = tarfile.TarInfo(name="script.py")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    container.put_archive("/tmp", buf)


def _cleanup_container(container: Any, client: Any) -> None:
    """Stop container, remove it, close client. Logs failures."""
    try:
        container.stop(timeout=2)
    except Exception:
        logger.warning("Container cleanup failed: stop", exc_info=True)
    try:
        container.remove(force=True)
    except Exception:
        logger.warning("Container cleanup failed: remove", exc_info=True)
    try:
        client.close()
    except Exception:
        logger.warning("Container cleanup failed: close", exc_info=True)


def make_contained_execute_tool(
    *,
    image: str,
    timeout_seconds: float = 30,
    max_output_bytes: int = 65536,
    socket: str | None = None,
    mem_limit: str = "256m",
    cpu_period: int = 100_000,
    cpu_quota: int = 50_000,
    pids_limit: int = 64,
    tmpfs_size: str = "64m",
    network_disabled: bool = True,
    read_only: bool = True,
) -> Tool:
    """Create container-isolated Python execution tool. Image must be pre-staged."""
    try:
        import docker as _docker_check  # noqa: F401  # Availability check only
    except ImportError:
        raise ImportError(
            "Container support requires docker SDK. Install with: pip install arcrun[container]"
        ) from None

    if "@sha256:" not in image:
        logger.warning(
            "Image %r lacks digest pin. Consider image@sha256:... for reproducibility.",
            image,
        )

    resolved_socket = socket or _detect_socket()

    def _create_container(client: Any) -> Any:
        """Create locked-down container. Separate from run for clarity."""
        return client.containers.create(
            image=image,
            command=["sleep", "infinity"],
            user="65534:65534",
            network_disabled=network_disabled,
            read_only=read_only,
            mem_limit=mem_limit,
            cpu_period=cpu_period,
            cpu_quota=cpu_quota,
            pids_limit=pids_limit,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            # noexec prevents binary drops; Python scripts still run via interpreter
            tmpfs={"/tmp": f"size={tmpfs_size},noexec,nosuid"},
            auto_remove=False,
        )

    def _run_in_container(container: Any, code: str) -> tuple[int, bytes, bytes]:
        """Start container, inject code, execute, return (exit_code, stdout, stderr)."""
        container.start()
        _inject_code_via_tar(container, code)
        exec_result = container.exec_run(["python", "/tmp/script.py"], demux=True)
        # demux=True returns (stdout, stderr) but either can be None
        # when the stream produced no output; the outer `or` guards
        # against .output itself being None on some SDK versions
        stdout_raw, stderr_raw = exec_result.output or (b"", b"")
        return (
            exec_result.exit_code,
            (stdout_raw or b"")[:max_output_bytes],
            (stderr_raw or b"")[:max_output_bytes],
        )

    def _sync_run(code: str) -> str:
        """Execute code in container. Runs in thread to avoid blocking event loop."""
        import docker as docker_sdk

        start = time.time()
        client = docker_sdk.DockerClient(base_url=resolved_socket)
        container = _create_container(client)
        try:
            exit_code, stdout, stderr = _run_in_container(container, code)
        finally:
            _cleanup_container(container, client)

        if exit_code == 137:
            raise SandboxOOMError("Container killed by OOM (exit code 137)")

        return json.dumps(
            {
                "stdout": stdout.decode(errors="replace"),
                "stderr": stderr.decode(errors="replace"),
                "exit_code": exit_code,
                "duration_ms": round((time.time() - start) * 1000, 1),
            }
        )

    async def _execute(params: dict[str, Any], ctx: ToolContext) -> str:
        code = params["code"]
        code_bytes = len(code.encode("utf-8"))
        if code_bytes > MAX_CODE_BYTES:
            raise SandboxRuntimeError(f"Code size {code_bytes} exceeds {MAX_CODE_BYTES} byte limit")
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_sync_run, code),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            raise SandboxTimeoutError(f"Execution exceeded {timeout_seconds}s timeout") from None

    return Tool(
        name="contained_execute_python",
        description="Execute Python in isolated container. "
        "No network, read-only FS, mem/cpu/pid limits.",
        input_schema={
            "type": "object",
            "properties": {"code": {"type": "string", "description": "Python code to execute"}},
            "required": ["code"],
        },
        execute=_execute,
        timeout_seconds=None,
    )
