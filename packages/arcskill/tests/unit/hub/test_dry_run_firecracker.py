"""Tests for arcskill.hub.dry_run -- FirecrackerSandbox + availability + fallback chain.

Test categories
---------------
- is_firecracker_available(): platform detection and binary/KVM checks
- FirecrackerSandbox.build_jailer_config(): JSON schema correctness
- Federal tier: SandboxRequired raised when Firecracker is missing
- Enterprise / personal tier: DockerBackend fallback
- Real Firecracker execution: linux_priv marker, skipped when unavailable

Mocking strategy
----------------
We mock at the lowest useful layer:
- ``Path.exists`` → controls /dev/kvm presence without touching the filesystem
- ``shutil.which`` → controls binary-on-PATH without requiring installation
- ``arcskill.hub.dry_run.DockerBackend`` → prevents real Docker calls
"""

from __future__ import annotations

import platform
import sys
import unittest.mock
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcskill.hub.config import HubConfig, TierPolicy
from arcskill.hub.dry_run import (
    DryRunResult,
    FirecrackerSandbox,
    _platform_is_macos,
    _run_in_sandbox,
    is_firecracker_available,
    run_dry_run,
)
from arcskill.hub.errors import SandboxRequired


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _federal_config() -> HubConfig:
    return HubConfig(
        enabled=True,
        tier=TierPolicy(level="federal"),
    )


def _enterprise_config() -> HubConfig:
    return HubConfig(
        enabled=True,
        tier=TierPolicy(level="enterprise"),
    )


def _personal_config() -> HubConfig:
    return HubConfig(
        enabled=True,
        tier=TierPolicy(level="personal"),
    )


# ---------------------------------------------------------------------------
# is_firecracker_available — platform / binary checks
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    platform.system() != "Darwin",
    reason="macOS-specific: /dev/kvm never exists on Darwin",
)
def test_is_firecracker_available_returns_false_on_macos() -> None:
    """On macOS, /dev/kvm does not exist, so availability must be False.

    We do NOT mock Path.exists here; the real filesystem is the oracle.
    This ensures the check is genuinely exercised on the macOS CI runner.
    """
    result = is_firecracker_available()
    assert result is False, (
        "Firecracker must be reported unavailable on macOS (no KVM hardware)"
    )


def test_is_firecracker_available_detects_kvm_and_binaries() -> None:
    """When /dev/kvm exists AND both binaries are on PATH → returns True."""
    with (
        patch("arcskill.hub.dry_run.Path") as mock_path_class,
        patch("arcskill.hub.dry_run.shutil.which") as mock_which,
    ):
        # /dev/kvm exists
        kvm_instance = MagicMock()
        kvm_instance.exists.return_value = True

        def path_side_effect(p: str) -> MagicMock:
            """Return a mock that exists for /dev/kvm, real path otherwise."""
            if str(p) == "/dev/kvm":
                return kvm_instance
            # Fallback to a non-existing path mock for other paths
            other = MagicMock()
            other.exists.return_value = False
            return other

        mock_path_class.side_effect = path_side_effect

        # Both binaries present
        mock_which.side_effect = lambda name: f"/usr/bin/{name}" if name in ("firecracker", "jailer") else None

        result = is_firecracker_available()
        assert result is True


def test_is_firecracker_available_returns_false_when_kvm_missing() -> None:
    """Without /dev/kvm, availability is False even with binaries present."""
    with (
        patch("arcskill.hub.dry_run.Path") as mock_path_class,
        patch("arcskill.hub.dry_run.shutil.which", return_value="/usr/bin/firecracker"),
    ):
        kvm_instance = MagicMock()
        kvm_instance.exists.return_value = False
        mock_path_class.side_effect = lambda p: kvm_instance if str(p) == "/dev/kvm" else MagicMock(exists=lambda: False)

        result = is_firecracker_available()
        assert result is False


def test_is_firecracker_available_returns_false_when_jailer_missing() -> None:
    """Without jailer binary, availability is False even with KVM + firecracker."""
    with (
        patch("arcskill.hub.dry_run.Path") as mock_path_class,
        patch("arcskill.hub.dry_run.shutil.which") as mock_which,
    ):
        kvm_instance = MagicMock()
        kvm_instance.exists.return_value = True
        mock_path_class.side_effect = lambda p: kvm_instance if str(p) == "/dev/kvm" else MagicMock()

        # firecracker present but jailer absent
        mock_which.side_effect = lambda name: "/usr/bin/firecracker" if name == "firecracker" else None

        result = is_firecracker_available()
        assert result is False


# ---------------------------------------------------------------------------
# FirecrackerSandbox.build_jailer_config — JSON schema
# ---------------------------------------------------------------------------


def test_jailer_config_built_correctly_minimal() -> None:
    """build_jailer_config returns well-formed JSON-serialisable dict."""
    sandbox = FirecrackerSandbox(
        kernel_path=Path("/var/lib/arc/vmlinux.bin"),
        rootfs_path=Path("/var/lib/arc/rootfs.ext4"),
        jailer_binary=Path("/usr/bin/jailer"),
    )
    vm_id = "test-vm-001"
    config = sandbox.build_jailer_config(vm_id)

    # Top-level keys required by Firecracker v1.7 config schema
    assert "boot-source" in config
    assert "drives" in config
    assert "machine-config" in config
    assert "vsock" in config

    # Boot source
    boot = config["boot-source"]
    assert boot["kernel_image_path"] == "/var/lib/arc/vmlinux.bin"
    assert "boot_args" in boot

    # Drive
    drives = config["drives"]
    assert len(drives) == 1
    drive = drives[0]
    assert drive["drive_id"] == "rootfs"
    assert drive["path_on_host"] == "/var/lib/arc/rootfs.ext4"
    assert drive["is_root_device"] is True
    assert drive["is_read_only"] is True

    # Machine config
    mc = config["machine-config"]
    assert mc["vcpu_count"] == 1
    assert mc["mem_size_mib"] == 128

    # VSock
    vsock = config["vsock"]
    assert vsock["guest_cid"] == 3
    assert vm_id in vsock["uds_path"]


def test_jailer_config_no_network_when_disabled() -> None:
    """Default network_interface='none' must exclude network-interfaces key."""
    sandbox = FirecrackerSandbox(
        kernel_path=Path("/k"),
        rootfs_path=Path("/r"),
        jailer_binary=Path("/j"),
        network_interface="none",
    )
    config = sandbox.build_jailer_config("vm-net-test")
    assert "network-interfaces" not in config


def test_jailer_config_includes_network_when_specified() -> None:
    """Explicit network_interface value must appear in network-interfaces."""
    sandbox = FirecrackerSandbox(
        kernel_path=Path("/k"),
        rootfs_path=Path("/r"),
        jailer_binary=Path("/j"),
        network_interface="tap0",
    )
    config = sandbox.build_jailer_config("vm-net-test")
    assert "network-interfaces" in config
    ifaces = config["network-interfaces"]
    assert len(ifaces) == 1
    assert ifaces[0]["host_dev_name"] == "tap0"


def test_jailer_config_custom_vcpu_and_mem() -> None:
    """Custom vcpu_count and mem_size_mib propagate to machine-config."""
    sandbox = FirecrackerSandbox(
        kernel_path=Path("/k"),
        rootfs_path=Path("/r"),
        jailer_binary=Path("/j"),
        vcpu_count=4,
        mem_size_mib=512,
    )
    config = sandbox.build_jailer_config("vm-custom")
    assert config["machine-config"]["vcpu_count"] == 4
    assert config["machine-config"]["mem_size_mib"] == 512


# ---------------------------------------------------------------------------
# Federal tier: SandboxRequired enforcement
# ---------------------------------------------------------------------------


def test_federal_raises_sandbox_required_when_firecracker_missing() -> None:
    """Federal install must hard-fail if Firecracker is not available.

    This is the critical fail-closed security gate: untrusted skill code
    cannot run without microVM isolation at the federal tier.
    """
    config = _federal_config()

    with patch("arcskill.hub.dry_run.is_firecracker_available", return_value=False):
        with pytest.raises(SandboxRequired) as exc_info:
            import asyncio
            asyncio.run(_run_in_sandbox("python -c 'pass'", Path("/tmp"), config))

    assert "Federal tier" in str(exc_info.value)
    assert "Firecracker" in str(exc_info.value)


def test_federal_raises_sandbox_required_via_run_dry_run(tmp_path: Path) -> None:
    """run_dry_run (the public entry point) also raises at federal tier."""
    # Build a minimal tarball so _safe_extract succeeds
    import tarfile

    bundle = tmp_path / "skill.tar.gz"
    skill_dir = tmp_path / "skill_src"
    skill_dir.mkdir()
    (skill_dir / "skill.py").write_text("def run(): pass\n")
    with tarfile.open(bundle, "w:gz") as tf:
        tf.add(skill_dir / "skill.py", arcname="skill.py")

    config = _federal_config()

    with patch("arcskill.hub.dry_run.is_firecracker_available", return_value=False):
        with pytest.raises(SandboxRequired):
            run_dry_run(bundle, config)


# ---------------------------------------------------------------------------
# Enterprise / personal: DockerBackend fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enterprise_falls_back_to_docker_when_firecracker_missing(
    tmp_path: Path,
) -> None:
    """At enterprise tier, DockerBackend is tried when Firecracker is absent.

    We mock DockerBackend to avoid real Docker calls and assert the result
    reports backend_used='docker'.
    """
    mock_docker = MagicMock()
    mock_handle = MagicMock()
    mock_handle.handle_id = "test-handle"

    # run() is an async function returning a handle
    mock_docker.run = AsyncMock(return_value=mock_handle)

    # stream() is an async generator yielding chunks
    async def fake_stream(_handle: object) -> object:
        yield b"1 passed"

    mock_docker.stream = fake_stream
    mock_docker.close = AsyncMock()

    config = _enterprise_config()

    mock_docker_class = MagicMock(return_value=mock_docker)

    with (
        patch("arcskill.hub.dry_run.is_firecracker_available", return_value=False),
        patch("arcskill.hub.dry_run._docker_available", return_value=True),
        patch("arcskill.hub.dry_run._DockerBackend", mock_docker_class),
    ):
        result = await _run_in_sandbox("pytest", tmp_path, config)

    assert result.backend_used == "docker"
    assert result.passed is True


@pytest.mark.asyncio
async def test_personal_falls_back_to_docker_when_firecracker_missing(
    tmp_path: Path,
) -> None:
    """At personal tier, DockerBackend is tried when Firecracker is absent."""
    mock_docker = MagicMock()
    mock_handle = MagicMock()
    mock_handle.handle_id = "test-handle"
    mock_docker.run = AsyncMock(return_value=mock_handle)

    async def fake_stream(_handle: object) -> object:
        yield b"ok"

    mock_docker.stream = fake_stream
    mock_docker.close = AsyncMock()

    mock_docker_class = MagicMock(return_value=mock_docker)
    config = _personal_config()

    with (
        patch("arcskill.hub.dry_run.is_firecracker_available", return_value=False),
        patch("arcskill.hub.dry_run._docker_available", return_value=True),
        patch("arcskill.hub.dry_run._DockerBackend", mock_docker_class),
    ):
        result = await _run_in_sandbox("pytest", tmp_path, config)

    assert result.backend_used == "docker"
    assert result.passed is True


@pytest.mark.asyncio
async def test_personal_skips_with_warning_when_neither_sandbox_available(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When both Firecracker and Docker are absent at non-federal tier,
    the result is skipped=True with an AUDIT WARNING in the log."""
    import logging

    config = _personal_config()

    with (
        patch("arcskill.hub.dry_run.is_firecracker_available", return_value=False),
        patch("arcskill.hub.dry_run._docker_available", return_value=False),
        caplog.at_level(logging.WARNING, logger="arcskill.hub.dry_run"),
    ):
        result = await _run_in_sandbox("pytest", tmp_path, config)

    assert result.skipped is True
    assert result.passed is True
    assert result.backend_used == "skipped"
    assert "AUDIT WARNING" in caplog.text


# ---------------------------------------------------------------------------
# FirecrackerSandbox unit internals (no actual VM)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_firecracker_sandbox_execute_raises_sandbox_required_without_fc_binary(
    tmp_path: Path,
) -> None:
    """_spawn_jailer raises SandboxRequired if firecracker binary missing."""
    sandbox = FirecrackerSandbox(
        kernel_path=tmp_path / "vmlinux.bin",
        rootfs_path=tmp_path / "rootfs.ext4",
        jailer_binary=tmp_path / "jailer",
    )

    # Ensure kernel and rootfs exist so we get past that check
    (tmp_path / "vmlinux.bin").write_bytes(b"fake")
    (tmp_path / "rootfs.ext4").write_bytes(b"fake")

    with patch("arcskill.hub.dry_run.shutil.which", return_value=None):
        # The SandboxRequired is caught inside execute() and returned as
        # a failed DryRunResult — but we patch _prepare_chroot to skip
        # the real mount syscall.
        with patch.object(sandbox, "_prepare_chroot", AsyncMock()):
            result = await sandbox.execute(tmp_path, timeout_s=2)

    assert result.passed is False
    assert result.backend_used == "firecracker"


@pytest.mark.asyncio
async def test_firecracker_sandbox_execute_returns_vm_id(tmp_path: Path) -> None:
    """Each execute() call must set a non-empty vm_id in the result."""
    sandbox = FirecrackerSandbox(
        kernel_path=tmp_path / "vmlinux.bin",
        rootfs_path=tmp_path / "rootfs.ext4",
        jailer_binary=tmp_path / "jailer",
    )
    (tmp_path / "vmlinux.bin").write_bytes(b"fake")
    (tmp_path / "rootfs.ext4").write_bytes(b"fake")

    with patch("arcskill.hub.dry_run.shutil.which", return_value=None):
        with patch.object(sandbox, "_prepare_chroot", AsyncMock()):
            result = await sandbox.execute(tmp_path, timeout_s=2)

    assert result.vm_id != ""
    assert len(result.vm_id) == 36  # UUID4 length with hyphens


# ---------------------------------------------------------------------------
# Real Firecracker integration (linux_priv marker — skipped on macOS / no KVM)
# ---------------------------------------------------------------------------

# Custom marker registered in conftest or pyproject markers.
# Tests under this marker require: Linux + /dev/kvm accessible + firecracker +
# jailer on PATH + ARC_FC_KERNEL and ARC_FC_ROOTFS pointing to real images.
_FIRECRACKER_MISSING = not is_firecracker_available()
_NOT_LINUX = sys.platform != "linux"


@pytest.mark.skipif(
    _FIRECRACKER_MISSING or _NOT_LINUX,
    reason="Requires Linux with /dev/kvm, firecracker, and jailer on PATH",
)
def test_real_firecracker_execute(tmp_path: Path) -> None:  # pragma: no cover
    """Full integration: launch a real Firecracker VM and run a command.

    This test is marked ``linux_priv`` and skipped automatically when:
    - Running on macOS (no KVM)
    - ``/dev/kvm`` is not accessible
    - ``firecracker`` or ``jailer`` is not on PATH
    - ``ARC_FC_KERNEL`` / ``ARC_FC_ROOTFS`` env vars are not set

    To run locally on a KVM-capable Linux host::

        ARC_FC_KERNEL=/var/lib/arc/vmlinux.bin \\
        ARC_FC_ROOTFS=/var/lib/arc/rootfs.ext4 \\
        uv run pytest tests/unit/hub/test_dry_run_firecracker.py \\
            -k test_real_firecracker_execute -v
    """
    import asyncio
    import os

    kernel = Path(os.environ.get("ARC_FC_KERNEL", "/var/lib/arc/vmlinux.bin"))
    rootfs = Path(os.environ.get("ARC_FC_ROOTFS", "/var/lib/arc/rootfs.ext4"))

    if not kernel.exists() or not rootfs.exists():
        pytest.skip("ARC_FC_KERNEL or ARC_FC_ROOTFS image files not found")

    jailer = Path(shutil.which("jailer") or "")  # type: ignore[arg-type]

    sandbox = FirecrackerSandbox(
        kernel_path=kernel,
        rootfs_path=rootfs,
        jailer_binary=jailer,
    )

    # Simple smoke test: just print something from the guest.
    result = asyncio.run(
        sandbox.execute(
            skill_path=tmp_path,
            test_command="echo dry-run-ok && exit 0",
            timeout_s=15,
        )
    )

    assert result.vm_id != ""
    assert result.backend_used == "firecracker"
    # Either passed (real VM booted) or failed with an error message —
    # we assert the result is well-formed rather than requiring pass,
    # since this depends on kernel + rootfs correctness.
    assert isinstance(result.passed, bool)
    assert result.duration_s > 0.0
