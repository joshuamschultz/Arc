"""VmBackend — hardware-isolated code execution via Firecracker microVM.

This is the ASI05 ("Unexpected Code Execution") enforcement surface Arc claims:
agent-generated code runs inside a microVM with its own guest kernel behind a
KVM boundary, not in a host subprocess or a shared-kernel container.

Engine seam (REQ-002)
---------------------
The isolation engine sits behind the tiny :class:`VmEngine` Protocol so it is a
drop-in swap. The default is :class:`FirecrackerEngine` (Firecracker over
``/dev/kvm``). ``gVisor``/``runsc`` (systrap) is the documented alternative that
satisfies the same Protocol shape — but note it is userspace-kernel isolation,
NOT hardware-VM class, so it does not meet the federal hardware-isolation floor
(SC-39(1)); it is a break-glass engine, never an automatic fallback.

Jailer + seccomp (Security)
---------------------------
Firecracker's isolation is only as strong as the jailer. The engine MUST launch
via the jailer (mount/pid/net namespaces, chroot/pivot_root, cgroups, drop
privileges to a non-root uid/gid, mknod only /dev/kvm) with seccomp level 2
(argument-constrained syscall allowlist) — never bare ``firecracker``. The one
2026 Firecracker CVE (CVE-2026-1386) was a jailer symlink bug, not a KVM escape.

Fail-closed (REQ-003)
---------------------
No ``/dev/kvm`` or a non-Linux host → :class:`VmUnavailableError`. The backend
NEVER substitutes a weaker path. This run-time probe is defence-in-depth; the
routing decision consumes an injected ``platform_supports_vm`` fact upstream.

Guest posture (REQ-004)
-----------------------
The guest reuses the container deny-by-default surface: no network, read-only
rootfs, non-root, pid/mem/cpu bounds, hard wall-clock timeout.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from arcrun.backends.base import (
    BackendCapabilities,
    ExecHandle,
    SeparatedResult,
)

_DEFAULT_MAX_STDOUT = 64 * 1024
_DEFAULT_KVM_PATH = "/dev/kvm"
# Non-root UID/GID the jailer drops the microVM process to (nobody:nogroup).
_JAILER_UID = 65534
_JAILER_GID = 65534
_SECCOMP_LEVEL = "2"  # argument-constrained syscall allowlist


class VmUnavailableError(Exception):
    """Raised when VM isolation is required but the host cannot provide it.

    A distinct, typed error (REQ-003): no ``/dev/kvm`` or a non-Linux host means
    execution refuses — the backend never falls back to a container or subprocess.
    """


@dataclass(frozen=True)
class VmGuestPolicy:
    """Deny-by-default guest posture, mirroring the container backend (REQ-004)."""

    network: bool = False
    read_only_rootfs: bool = True
    vcpu_count: int = 1
    mem_limit_mib: int = 256
    pids_limit: int = 64


@runtime_checkable
class VmEngine(Protocol):
    """Pluggable microVM launch engine (REQ-002).

    Firecracker is the default; gVisor/``runsc`` is the documented alternative
    satisfying the same shape. ``build_launch_argv`` returns the fully-formed
    argv used to spawn the isolated guest — it must encode the privilege-drop
    and seccomp posture so callers cannot bypass it.
    """

    name: str

    def build_launch_argv(
        self,
        *,
        jailer_id: str,
        chroot_base: str,
        uid: int,
        gid: int,
        config_file: str,
    ) -> list[str]: ...


class FirecrackerEngine:
    """Launch Firecracker via the jailer with seccomp level 2 (never bare).

    The jailer sets up namespaces, chroot/pivot_root, cgroups and drops
    privileges to ``uid``/``gid`` before exec'ing Firecracker, which reads its
    machine config (rootfs read-only, no network, mem/vcpu bounds) from
    ``config_file``. Everything after ``--`` is passed to Firecracker itself.
    """

    name: str = "firecracker"

    def __init__(
        self,
        *,
        jailer_bin: str = "jailer",
        firecracker_bin: str = "/usr/bin/firecracker",
    ) -> None:
        self._jailer_bin = jailer_bin
        self._firecracker_bin = firecracker_bin

    def build_launch_argv(
        self,
        *,
        jailer_id: str,
        chroot_base: str,
        uid: int,
        gid: int,
        config_file: str,
    ) -> list[str]:
        """Build the jailer argv that launches a hardened Firecracker microVM."""
        return [
            self._jailer_bin,
            "--id",
            jailer_id,
            "--exec-file",
            self._firecracker_bin,
            "--uid",
            str(uid),
            "--gid",
            str(gid),
            "--chroot-base-dir",
            chroot_base,
            "--",
            "--seccomp-level",
            _SECCOMP_LEVEL,
            "--config-file",
            config_file,
        ]


class VmBackend:
    """Execute code inside a Firecracker microVM (isolation="vm").

    Shared-nothing per execution: each run() launches an independent guest.
    Fail-closed when ``/dev/kvm`` is absent or the host is not Linux.
    """

    name: str = "vm"
    capabilities: BackendCapabilities

    def __init__(
        self,
        *,
        engine: VmEngine | None = None,
        max_stdout_bytes: int = _DEFAULT_MAX_STDOUT,
        kvm_path: str = _DEFAULT_KVM_PATH,
        chroot_base: str = "/srv/jailer",
        policy: VmGuestPolicy | None = None,
    ) -> None:
        self._engine: VmEngine = engine or FirecrackerEngine()
        self._kvm_path = kvm_path
        self._chroot_base = chroot_base
        self._policy = policy or VmGuestPolicy()
        self.capabilities = BackendCapabilities(
            supports_file_copy=False,
            supports_persistent_workspace=False,
            supports_port_forward=False,
            supports_bind_mount=False,
            supports_separated_streams=True,
            # Cold Firecracker boot ~125-200ms; below the container backend's 800ms.
            # A pre-warmed snapshot pool drops this to ~10-30ms (fleet follow-up).
            cold_start_budget_ms=200,
            max_stdout_bytes=max_stdout_bytes,
            isolation="vm",
        )

    # ------------------------------------------------------------------
    # Availability (fail-closed, REQ-003)
    # ------------------------------------------------------------------

    def available(self) -> bool:
        """True only on Linux with an accessible ``/dev/kvm``.

        Defence-in-depth run-time probe. The router's ``platform_supports_vm``
        is the authoritative routing input; this guards the execution path too.
        """
        return sys.platform.startswith("linux") and Path(self._kvm_path).exists()

    def _ensure_available(self) -> None:
        if not self.available():
            raise VmUnavailableError(
                f"VM isolation unavailable: requires Linux with {self._kvm_path}. "
                f"platform={sys.platform!r}. Refusing to substitute a weaker path."
            )

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
        """Launch a microVM guest. Fails closed if KVM is unavailable."""
        result = await self.run_separated(
            command, cwd=cwd, env=env, timeout=timeout, stdin=stdin
        )
        return ExecHandle(
            handle_id=self._jailer_id(),
            backend_name=self.name,
            meta={"exit_code": result.exit_code},
        )

    async def run_separated(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 120.0,
        stdin: str | None = None,
    ) -> SeparatedResult:
        """Boot a microVM, run ``command`` inside it, return separated output.

        Fails closed (VmUnavailableError) when the hypervisor is absent. On a
        provisioned KVM host the jailer launches Firecracker with the guest's
        deny-by-default posture; provisioning the guest kernel + rootfs is a
        federal-deployment prerequisite (documented in the SDD).
        """
        self._ensure_available()
        # One id per run: the jailer ``--id`` and the config path MUST agree, or
        # the jailer chroots to a directory that has no vmconfig.json.
        jailer_id = self._jailer_id()
        argv = self._engine.build_launch_argv(
            jailer_id=jailer_id,
            chroot_base=self._chroot_base,
            uid=_JAILER_UID,
            gid=_JAILER_GID,
            config_file=self._write_vmconfig(jailer_id, command, cwd=cwd, env=env),
        )
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        max_bytes = self.capabilities.max_stdout_bytes
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return SeparatedResult(stdout=b"", stderr=b"Error: execution timed out", exit_code=-1)
        return SeparatedResult(
            stdout=stdout_b[:max_bytes],
            stderr=stderr_b[:max_bytes],
            exit_code=proc.returncode if proc.returncode is not None else -1,
        )

    async def stream(self, handle: ExecHandle) -> AsyncIterator[bytes]:
        """VM execution is collected, not streamed; yields nothing.

        The separated-result path (run_separated) is the supported surface.
        """
        return
        yield b""  # pragma: no cover — satisfy the async-generator type

    async def cancel(self, handle: ExecHandle, *, grace: float = 5.0) -> None:
        """No-op: run_separated fully awaits the guest before returning."""

    async def close(self) -> None:
        """No persistent resources to release; each run is shared-nothing."""

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _jailer_id(self) -> str:
        return f"arc-vm-{uuid.uuid4().hex[:12]}"

    def _write_vmconfig(
        self,
        jailer_id: str,
        command: str,
        *,
        cwd: str | None,
        env: dict[str, str] | None,
    ) -> str:
        """Render the deny-by-default Firecracker machine-config into the chroot.

        The config encodes ``self._policy`` as the machine spec — mem/vcpu caps,
        read-only rootfs, and no network interfaces (egress denied). The guest
        payload (``command``, working dir, env, pids cap) rides the kernel cmdline
        via ``boot_args``; the guest init reads /proc/cmdline to launch it. Written
        under ``<chroot_base>/<jailer_id>/root/`` so the path matches the jailer
        ``--id``. Reached only after the KVM gate passes, so the chroot write
        happens solely on a provisioned host.
        """
        policy = self._policy
        root = Path(self._chroot_base) / jailer_id / "root"
        root.mkdir(parents=True, exist_ok=True)
        config = {
            "boot-source": {
                "kernel_image_path": "vmlinux",
                "boot_args": _guest_boot_args(
                    command, cwd=cwd, env=env, pids_limit=policy.pids_limit
                ),
            },
            "drives": [
                {
                    "drive_id": "rootfs",
                    "path_on_host": "rootfs.ext4",
                    "is_root_device": True,
                    "is_read_only": policy.read_only_rootfs,
                }
            ],
            "machine-config": {
                "vcpu_count": policy.vcpu_count,
                "mem_size_mib": policy.mem_limit_mib,
                "smt": False,
            },
            # Deny-by-default egress: no NICs unless the policy explicitly enables
            # the guest network (it never does at any Arc tier).
            "network-interfaces": (
                [] if not policy.network else [{"iface_id": "eth0", "host_dev_name": "arc-tap0"}]
            ),
        }
        config_path = root / "vmconfig.json"
        config_path.write_text(json.dumps(config, indent=2, sort_keys=True))
        return str(config_path)


def _guest_boot_args(
    command: str,
    *,
    cwd: str | None,
    env: dict[str, str] | None,
    pids_limit: int,
) -> str:
    """Kernel cmdline carrying the guest payload the init reads from /proc/cmdline.

    The command, working dir, env, and pids cap are base64-JSON-encoded into a
    single ``arc_payload`` token so arbitrary code/values never split the cmdline.
    """
    payload = {
        "cmd": command,
        "cwd": cwd or "/tmp",
        "env": env or {},
        "pids_limit": pids_limit,
    }
    encoded = base64.b64encode(json.dumps(payload, sort_keys=True).encode()).decode()
    return f"console=ttyS0 reboot=k panic=1 pci=off arc_payload={encoded}"


__all__ = [
    "FirecrackerEngine",
    "VmBackend",
    "VmEngine",
    "VmGuestPolicy",
    "VmUnavailableError",
]
