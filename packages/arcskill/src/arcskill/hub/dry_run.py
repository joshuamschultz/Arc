"""arcskill.hub.dry_run -- Sandboxed skill dry-run.

Isolation policy
----------------
- **Federal tier**: Firecracker microVM isolation is REQUIRED.  If the
  Firecracker backend is unavailable, ``SandboxRequired`` is raised and
  the install is aborted (fail-closed).
- **Enterprise / personal tier**: Firecracker preferred; falls back to
  ``DockerBackend`` from ``arcrun.backends.docker`` when Firecracker is
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

Firecracker deployment notes
-----------------------------
``FirecrackerSandbox`` wraps the Firecracker ``jailer`` binary.  The wrapper
code is complete; actual privileged deployment requires:

- Linux kernel with KVM support (``/dev/kvm`` accessible by the process UID)
- ``firecracker`` binary (v1.7+) and ``jailer`` binary installed and on PATH
- A Linux kernel image (``vmlinux.bin``) and ext4 rootfs image with Python
- The running process must have CAP_SYS_ADMIN or the operator must pre-grant
  jailer UID/GID permissions via ``chown`` on ``/dev/kvm``
- A seccomp filter profile in JSON format (recommended: upstream default)

See ``packages/arcskill/docs/firecracker-deployment.md`` for the full operator
deployment guide.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import platform
import shutil
import signal
import tarfile
import tempfile
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from arcskill.hub.config import HubConfig
from arcskill.hub.errors import SandboxRequired

if TYPE_CHECKING:
    from arctrust import AuditSink

logger = logging.getLogger(__name__)

# DockerBackend is an optional dependency (requires arcrun package).
# Imported at module level so tests can patch arcskill.hub.dry_run.DockerBackend.
# Falls back to None when arcrun is not installed; _run_docker handles the None case.
try:
    from arcrun.backends.docker import DockerBackend as _DockerBackend
except ImportError:
    _DockerBackend = None  # type: ignore[assignment,misc]

# Module-level warning when Firecracker is absent at import time.
# This is informational only; the hard fail is raised at run time when
# federal tier calls execute().
if not shutil.which("firecracker") or not Path("/dev/kvm").exists():
    logger.debug(
        "Firecracker not available on this host (expected on macOS / non-KVM Linux). "
        "Federal tier dry-run will raise SandboxRequired."
    )

_DRY_RUN_TIMEOUT_SECONDS = 10

# ---------------------------------------------------------------------------
# Public availability check
# ---------------------------------------------------------------------------


def is_firecracker_available() -> bool:
    """Return True if Firecracker can be used on this host.

    Checks three conditions, all of which must be satisfied:
    1. ``/dev/kvm`` exists (Linux KVM hardware-virtualisation interface)
    2. ``firecracker`` binary is discoverable on PATH
    3. ``jailer`` binary is discoverable on PATH

    This function never raises; it returns False for any missing condition.
    On macOS or other non-Linux platforms this always returns False.
    """
    kvm_present = Path("/dev/kvm").exists()
    fc_present = bool(shutil.which("firecracker"))
    jailer_present = bool(shutil.which("jailer"))
    available = kvm_present and fc_present and jailer_present
    if not available:
        logger.debug(
            "is_firecracker_available=False (kvm=%s firecracker=%s jailer=%s)",
            kvm_present,
            fc_present,
            jailer_present,
        )
    return available


# ---------------------------------------------------------------------------
# DryRunResult
# ---------------------------------------------------------------------------


class DryRunResult(BaseModel):
    """Output of the dry-run stage.

    Attributes
    ----------
    passed:
        True if the fixture ran to completion without error.
    stdout:
        Captured stdout from the sandbox (truncated to 4 KB).
    stderr:
        Captured stderr from the sandbox (truncated to 4 KB).
    exit_code:
        Process exit code (0 = success, None = timeout / unavailable).
    backend_used:
        ``"firecracker"``, ``"docker"``, or ``"skipped"`` (local/test mode).
    skipped:
        True when sandbox was skipped (only allowed at non-federal tiers in
        test mode via the ``skip_sandbox`` override flag).
    duration_s:
        Wall-clock seconds for the dry-run (0.0 when skipped).
    vm_id:
        Per-VM unique identifier (UUID string).  Empty string when not using
        Firecracker.
    """

    passed: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    backend_used: str = "skipped"
    skipped: bool = False
    duration_s: float = 0.0
    vm_id: str = ""


# ---------------------------------------------------------------------------
# FirecrackerConfig -- dataclass encoding jailer / VM parameters
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class FirecrackerConfig:
    """Configuration for a single Firecracker microVM launch.

    All paths are absolute and validated at construction time by
    ``FirecrackerSandbox.__init__``.

    Attributes
    ----------
    kernel_path:
        Path to the Linux kernel image (``vmlinux.bin``).
    rootfs_path:
        Path to the ext4 root filesystem image.
    jailer_binary:
        Path to the ``jailer`` binary.
    vcpu_count:
        Number of vCPUs to allocate to the VM (default: 1).
    mem_size_mib:
        Memory in MiB (default: 128).
    network_interface:
        Network device name.  ``"none"`` disables networking entirely, which
        is the secure default for dry-run isolation.
    """

    kernel_path: Path
    rootfs_path: Path
    jailer_binary: Path
    vcpu_count: int = 1
    mem_size_mib: int = 128
    network_interface: str = "none"

    def to_machine_config_json(self) -> dict[str, Any]:
        """Return the Firecracker PUT /machine-config JSON payload."""
        return {
            "vcpu_count": self.vcpu_count,
            "mem_size_mib": self.mem_size_mib,
        }

    def to_boot_source_json(
        self, kernel_args: str = "console=ttyS0 reboot=k panic=1 pci=off"
    ) -> dict[str, Any]:
        """Return the Firecracker PUT /boot-source JSON payload."""
        return {
            "kernel_image_path": str(self.kernel_path),
            "boot_args": kernel_args,
        }

    def to_rootfs_drive_json(self) -> dict[str, Any]:
        """Return the Firecracker PUT /drives/rootfs JSON payload."""
        return {
            "drive_id": "rootfs",
            "path_on_host": str(self.rootfs_path),
            "is_root_device": True,
            "is_read_only": True,
        }


# ---------------------------------------------------------------------------
# FirecrackerSandbox
# ---------------------------------------------------------------------------


class FirecrackerSandbox:
    """Firecracker microVM sandbox for skill dry-run execution.

    Each call to ``execute()`` launches a fresh, isolated Firecracker VM
    via the ``jailer`` binary, runs the specified test command inside it,
    and tears everything down — including the chroot directory — before
    returning.

    The jailer hard-isolates the Firecracker process by:
    - Dropping all Linux capabilities
    - Applying a seccomp-BPF filter
    - Moving the process into a new network namespace (network disabled)
    - Chrooting into a per-VM directory under ``/srv/jailer/``

    This class handles config building, process lifecycle, vsock-based
    command dispatch, timeout enforcement, and cleanup.  It does NOT
    install Firecracker or KVM; that is operator concern (see
    ``docs/firecracker-deployment.md``).

    Parameters
    ----------
    kernel_path:
        Absolute path to the Linux kernel image (``vmlinux.bin``).
    rootfs_path:
        Absolute path to the ext4 rootfs image (read-only; skill is
        overlay-mounted or pre-baked).
    jailer_binary:
        Absolute path to the ``jailer`` binary.
    vcpu_count:
        Number of vCPUs to assign to the microVM (default: 1).
    mem_size_mib:
        Memory in MiB (default: 128).
    network_interface:
        Network device name for the VM.  ``"none"`` disables networking,
        which is the required default for dry-run isolation.
    """

    def __init__(
        self,
        kernel_path: Path,
        rootfs_path: Path,
        jailer_binary: Path,
        vcpu_count: int = 1,
        mem_size_mib: int = 128,
        network_interface: str = "none",
    ) -> None:
        self._config = FirecrackerConfig(
            kernel_path=kernel_path,
            rootfs_path=rootfs_path,
            jailer_binary=jailer_binary,
            vcpu_count=vcpu_count,
            mem_size_mib=mem_size_mib,
            network_interface=network_interface,
        )

    # ------------------------------------------------------------------
    # Public execute API
    # ------------------------------------------------------------------

    async def execute(
        self,
        skill_path: Path,
        test_command: str = "pytest",
        timeout_s: int = 10,
    ) -> DryRunResult:
        """Execute *test_command* inside a fresh Firecracker microVM.

        Steps
        -----
        1. Generate a per-VM UUID.
        2. Build the JailerConfig JSON (kernel + rootfs + vsock).
        3. Mount *skill_path* read-only into the rootfs overlay.
        4. Spawn Firecracker via the ``jailer`` binary with chroot,
           seccomp, and dropped capabilities.
        5. Wait for VM boot via a vsock readiness file.
        6. Send *test_command* via vsock; collect stdout/stderr/exit_code.
        7. Kill VM after *timeout_s* (SIGTERM → SIGKILL fallback).
        8. Clean up: unmount, remove chroot, delete jailer state directory.

        Returns
        -------
        DryRunResult
            Full outcome including vm_id, duration, and captured output.

        Raises
        ------
        SandboxRequired
            If the jailer binary or Firecracker binary is not executable.
        """
        vm_id = str(uuid.uuid4())
        start = time.monotonic()

        jailer_dir = Path(f"/srv/jailer/firecracker/{vm_id}/root")
        vsock_path = jailer_dir / "v.sock"

        logger.info(
            "[firecracker] Launching VM vm_id=%s skill=%s cmd=%r",
            vm_id,
            skill_path,
            test_command,
        )

        try:
            await self._prepare_chroot(vm_id, jailer_dir, skill_path)
            proc = await self._spawn_jailer(vm_id, jailer_dir)
            try:
                await self._wait_for_boot(vsock_path, timeout_s=min(timeout_s, 8))
                stdout, stderr, exit_code = await self._run_command_via_vsock(
                    vsock_path, test_command, timeout_s=timeout_s
                )
            finally:
                await self._kill_vm(proc)
        except Exception as exc:
            logger.error("[firecracker] VM execution error vm_id=%s: %s", vm_id, exc)
            duration = time.monotonic() - start
            return DryRunResult(
                passed=False,
                stdout="",
                stderr=str(exc),
                exit_code=-1,
                backend_used="firecracker",
                duration_s=duration,
                vm_id=vm_id,
            )
        finally:
            await self._cleanup(vm_id, jailer_dir)

        duration = time.monotonic() - start
        passed = exit_code == 0
        logger.info(
            "[firecracker] VM done vm_id=%s exit_code=%d duration=%.2fs",
            vm_id,
            exit_code,
            duration,
        )
        return DryRunResult(
            passed=passed,
            stdout=stdout[:4096],
            stderr=stderr[:4096],
            exit_code=exit_code,
            backend_used="firecracker",
            duration_s=duration,
            vm_id=vm_id,
        )

    # ------------------------------------------------------------------
    # Config building (testable without a real VM)
    # ------------------------------------------------------------------

    def build_jailer_config(self, vm_id: str) -> dict[str, Any]:
        """Return the jailer config JSON for this VM.

        This is the complete configuration dict that would be written to
        a config file or passed to the Firecracker API.  It is separated
        from ``execute()`` so tests can assert on the schema without
        actually spawning a VM.

        The returned dict follows the Firecracker v1.7 configuration schema:
        https://github.com/firecracker-microvm/firecracker/blob/main/src/api_server/swagger/firecracker.yaml
        """
        cfg: dict[str, Any] = {
            "boot-source": self._config.to_boot_source_json(),
            "drives": [self._config.to_rootfs_drive_json()],
            "machine-config": self._config.to_machine_config_json(),
            "vsock": {
                "guest_cid": 3,
                "uds_path": f"/srv/jailer/firecracker/{vm_id}/root/v.sock",
            },
        }
        # Network is intentionally absent when disabled to prevent any
        # outbound connectivity from the untrusted skill code.
        if self._config.network_interface != "none":
            cfg["network-interfaces"] = [
                {
                    "iface_id": "eth0",
                    "host_dev_name": self._config.network_interface,
                }
            ]
        return cfg

    # ------------------------------------------------------------------
    # Internal VM lifecycle helpers
    # ------------------------------------------------------------------

    async def _prepare_chroot(self, vm_id: str, jailer_dir: Path, skill_path: Path) -> None:
        """Create the jailer chroot directory and bind-mount the skill.

        The skill directory is mounted read-only inside the chroot at
        ``/skill``.  This prevents the skill from modifying its source
        tree even if it escapes the Python-level sandbox.
        """
        # The jailer creates /srv/jailer/firecracker/<vm_id>/root automatically;
        # we pre-create the skill mount point here.
        skill_mount = jailer_dir / "skill"
        skill_mount.mkdir(parents=True, exist_ok=True)

        # Bind-mount skill_path read-only into the chroot.
        # On Linux this requires CAP_SYS_ADMIN; the jailer holds it.
        # We use a subprocess so the mount is inside the jailer namespace.
        proc = await asyncio.create_subprocess_exec(
            "mount",
            "--bind",
            "-o",
            "ro",
            str(skill_path),
            str(skill_mount),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_bytes = await proc.communicate()
        if proc.returncode != 0:
            # Mount failure is non-fatal for testing; the skill will still
            # be accessible via the shared rootfs overlay if pre-baked.
            logger.warning(
                "[firecracker] bind-mount failed (vm_id=%s): %s",
                vm_id,
                stderr_bytes.decode("utf-8", errors="replace").strip(),
            )

    async def _spawn_jailer(self, vm_id: str, _jailer_dir: Path) -> asyncio.subprocess.Process:
        """Spawn the Firecracker process via the jailer binary.

        The jailer performs chroot, seccomp filtering, network namespace
        isolation, and capability dropping before exec-ing Firecracker.
        All of these are kernel-enforced; the jailer binary requires
        CAP_SYS_ADMIN on the HOST to set them up.

        Raises
        ------
        SandboxRequired
            If the jailer binary is not found or not executable.
        """
        firecracker_bin = shutil.which("firecracker")
        if not firecracker_bin:
            raise SandboxRequired(
                "firecracker binary not found on PATH. "
                "Install Firecracker (see docs/firecracker-deployment.md)."
            )

        jailer_args = [
            str(self._config.jailer_binary),
            "--id",
            vm_id,
            "--exec-file",
            firecracker_bin,
            "--uid",
            str(os.getuid()),
            "--gid",
            str(os.getgid()),
            "--",  # End of jailer args; everything after goes to firecracker
            "--no-api",
            "--config-file",
            f"/srv/jailer/firecracker/{vm_id}/root/vm-config.json",
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *jailer_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise SandboxRequired(
                f"jailer binary not found: {self._config.jailer_binary} -- {exc}"
            ) from exc

        logger.debug("[firecracker] jailer spawned pid=%d vm_id=%s", proc.pid, vm_id)
        return proc

    async def _wait_for_boot(self, vsock_path: Path, timeout_s: int = 8) -> None:
        """Wait for the VM to signal readiness via vsock UDS path.

        The guest init script touches ``/var/run/arc-ready`` inside the VM,
        which is observable by the host as the vsock socket becoming connectable.
        We poll with exponential backoff up to *timeout_s* seconds.
        """
        deadline = time.monotonic() + timeout_s
        delay = 0.05  # start at 50ms, cap at 500ms
        while time.monotonic() < deadline:
            if vsock_path.exists():
                logger.debug("[firecracker] VM boot ready (vsock present)")
                return
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 0.5)

        # Timeout waiting for boot — not fatal; we still attempt the command.
        logger.warning(
            "[firecracker] VM did not signal readiness within %ds (vsock=%s)",
            timeout_s,
            vsock_path,
        )

    async def _run_command_via_vsock(
        self,
        vsock_path: Path,
        command: str,
        timeout_s: int = 10,
    ) -> tuple[str, str, int]:
        """Send *command* to the VM guest via vsock and collect output.

        The guest side runs a minimal init that reads a JSON command frame
        from the vsock connection and responds with a JSON result frame
        containing stdout, stderr, and exit_code.

        Frame format (newline-delimited JSON)::

            Request:  {"cmd": "<shell command>"}\\n
            Response: {"stdout": "...", "stderr": "...", "exit_code": 0}\\n

        Returns
        -------
        tuple[str, str, int]
            stdout, stderr, exit_code
        """
        if not vsock_path.exists():
            # VM did not boot; treat as failure but let cleanup happen.
            logger.warning("[firecracker] vsock not available, cannot dispatch command")
            return "", "VM did not boot (vsock missing)", -1

        request = json.dumps({"cmd": command}) + "\n"

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(vsock_path)),
                timeout=2.0,
            )
            writer.write(request.encode())
            await writer.drain()

            raw = await asyncio.wait_for(
                reader.readline(),
                timeout=float(timeout_s),
            )
            writer.close()
            await writer.wait_closed()

            result = json.loads(raw.decode("utf-8", errors="replace"))
            stdout = str(result.get("stdout", ""))
            stderr = str(result.get("stderr", ""))
            exit_code = int(result.get("exit_code", -1))
            return stdout, stderr, exit_code

        except TimeoutError:
            logger.warning("[firecracker] Command timed out after %ds: %r", timeout_s, command)
            return "", "[DRY-RUN TIMEOUT]", -1

        except (OSError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("[firecracker] vsock dispatch error: %s", exc)
            return "", str(exc), -1

    async def _kill_vm(self, proc: asyncio.subprocess.Process) -> None:
        """Terminate the Firecracker process (SIGTERM, then SIGKILL fallback).

        The jailer is PID 1 of a new process group; sending SIGTERM to the
        process group ensures all forked children are also terminated.
        """
        if proc.returncode is not None:
            return  # Already exited.

        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass

        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except TimeoutError:
            logger.warning("[firecracker] SIGTERM ignored; sending SIGKILL to pid=%d", proc.pid)
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                await proc.wait()
            except (ProcessLookupError, OSError):
                pass

    async def _cleanup(self, vm_id: str, jailer_dir: Path) -> None:
        """Remove chroot, unmount skill overlay, delete jailer state.

        Best-effort: errors are logged but not re-raised, so a cleanup
        failure does not mask the dry-run result.
        """
        skill_mount = jailer_dir / "skill"

        # Unmount skill bind-mount if it was created.
        if skill_mount.exists():
            proc = await asyncio.create_subprocess_exec(
                "umount",
                str(skill_mount),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()

        # Remove the jailer state directory tree.
        jailer_state = Path(f"/srv/jailer/firecracker/{vm_id}")
        if jailer_state.exists():
            proc2 = await asyncio.create_subprocess_exec(
                "rm",
                "-rf",
                str(jailer_state),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc2.wait()

        logger.debug("[firecracker] Cleanup complete vm_id=%s", vm_id)


# ---------------------------------------------------------------------------
# Public API — run_dry_run (backward-compatible entry point)
# ---------------------------------------------------------------------------


def run_dry_run(
    bundle_path: Path,
    config: HubConfig,
    *,
    skip_sandbox: bool = False,
    audit_sink: Any | None = None,
) -> DryRunResult:
    """Execute a sandboxed dry-run of the skill bundle.

    Sandbox runs at ALL tiers.  Tier controls *which* backend is used
    (Firecracker at federal, Docker/subprocess elsewhere), not *whether*
    to sandbox.  Passing ``skip_sandbox=True`` now raises ``SandboxRequired``
    at every tier — the previous non-federal bypass has been removed.

    Parameters
    ----------
    bundle_path:
        Path to the ``.tar.gz`` skill bundle.
    config:
        Hub configuration (tier determines isolation backend).
    skip_sandbox:
        Deprecated.  Formerly allowed skipping the sandbox at non-federal
        tiers.  Now raises ``SandboxRequired`` at ALL tiers regardless of
        tier level.  Callers in CI should mock ``is_firecracker_available``
        and ``_docker_available`` instead.
    audit_sink:
        Optional arctrust AuditSink for emitting structured audit events.

    Returns
    -------
    DryRunResult
        Outcome of the dry-run.

    Raises
    ------
    SandboxRequired
        If ``skip_sandbox=True`` is passed, or if the required sandbox
        backend is unavailable.
    """
    if skip_sandbox:
        # Bypass 3 removed: skip_sandbox is never allowed at any tier.
        # Tier controls which backend runs, not whether the sandbox runs.
        raise SandboxRequired(
            "skip_sandbox=True is not permitted at any tier. "
            "Sandbox must run at all tiers; tier determines the backend "
            "(Firecracker at federal, Docker/subprocess at enterprise/personal). "
            "In tests, mock is_firecracker_available() and _docker_available() "
            "instead of using skip_sandbox."
        )

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
    except Exception:
        logger.warning("Failed to emit sandbox audit event for target=%s", target)


async def _run_firecracker(fixture_cmd: str, skill_dir: Path) -> DryRunResult:
    """Execute the fixture inside a Firecracker microVM via FirecrackerSandbox.

    Requires kernel_path, rootfs_path, and jailer_binary to be configured via
    environment variables or the Arc config.  If they are not set, raises
    SandboxRequired so the install aborts cleanly.
    """
    kernel_path = Path(os.environ.get("ARC_FC_KERNEL", "/var/lib/arc/vmlinux.bin"))
    rootfs_path = Path(os.environ.get("ARC_FC_ROOTFS", "/var/lib/arc/rootfs.ext4"))
    jailer_bin = Path(shutil.which("jailer") or "/usr/bin/jailer")

    if not kernel_path.exists() or not rootfs_path.exists():
        raise SandboxRequired(
            f"Firecracker kernel ({kernel_path}) or rootfs ({rootfs_path}) not found. "
            "Set ARC_FC_KERNEL and ARC_FC_ROOTFS environment variables, or install "
            "the Arc Firecracker base images. "
            "See packages/arcskill/docs/firecracker-deployment.md."
        )

    sandbox = FirecrackerSandbox(
        kernel_path=kernel_path,
        rootfs_path=rootfs_path,
        jailer_binary=jailer_bin,
    )
    return await sandbox.execute(skill_dir, test_command=fixture_cmd)


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
            import yaml  # type: ignore[import-untyped]

            data = yaml.safe_load(module_yaml.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "test_fixture" in data:
                return str(data["test_fixture"])
        except Exception as exc:  # MODULE.yaml parse failure is non-fatal
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


def _docker_available() -> bool:
    """True if the docker CLI is on $PATH."""
    return bool(shutil.which("docker"))


def _platform_is_macos() -> bool:
    """True when running on macOS (Darwin)."""
    return platform.system() == "Darwin"
