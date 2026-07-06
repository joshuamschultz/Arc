"""Unit tests for VmBackend — Protocol conformance, capabilities, fail-closed probe.

The real Firecracker microVM cannot boot here (macOS / no /dev/kvm), so these
tests exercise the parts that are host-independent:

- Protocol conformance and declared capabilities (isolation="vm").
- The fail-closed availability probe: no /dev/kvm or non-Linux → VmUnavailableError,
  never a weaker substitute.
- The jailer+seccomp-L2 launch-argv construction (the security-critical seam),
  asserted without booting anything.

The live boot/exec test lives in tests/integration and is skip-guarded on /dev/kvm.
"""

from __future__ import annotations

import pytest

from arcrun.backends import ExecutorBackend
from arcrun.backends.vm import FirecrackerEngine, VmBackend, VmUnavailableError


class TestVmBackendProtocol:
    def test_is_executor_backend(self) -> None:
        assert isinstance(VmBackend(), ExecutorBackend)

    def test_name(self) -> None:
        assert VmBackend().name == "vm"

    def test_isolation_is_vm(self) -> None:
        assert VmBackend().capabilities.isolation == "vm"

    def test_cold_start_budget_is_realistic(self) -> None:
        # Cold Firecracker boot is ~125-200ms — below the container backend's 800ms.
        budget = VmBackend().capabilities.cold_start_budget_ms
        assert 100 <= budget <= 500

    def test_supports_separated_streams(self) -> None:
        assert VmBackend().capabilities.supports_separated_streams is True


class TestVmBackendFailClosed:
    """REQ-003: no /dev/kvm or non-Linux → typed VmUnavailableError, never weaker path."""

    def test_probe_false_on_missing_kvm(self) -> None:
        backend = VmBackend(kvm_path="/nonexistent/dev/kvm")
        assert backend.available() is False

    @pytest.mark.asyncio
    async def test_run_separated_fails_closed(self) -> None:
        backend = VmBackend(kvm_path="/nonexistent/dev/kvm")
        with pytest.raises(VmUnavailableError):
            await backend.run_separated("python3 -c 'print(1)'")

    @pytest.mark.asyncio
    async def test_run_fails_closed(self) -> None:
        backend = VmBackend(kvm_path="/nonexistent/dev/kvm")
        with pytest.raises(VmUnavailableError):
            await backend.run("python3 -c 'print(1)'")

    def test_vm_unavailable_error_is_typed(self) -> None:
        # A distinct exception type so no weaker path can be silently substituted.
        assert issubclass(VmUnavailableError, Exception)


class TestFirecrackerJailerArgv:
    """The engine MUST launch via jailer + seccomp level 2 — never bare firecracker."""

    def test_launches_via_jailer_not_bare_firecracker(self) -> None:
        argv = FirecrackerEngine().build_launch_argv(
            jailer_id="run-abc",
            chroot_base="/srv/jailer",
            uid=65534,
            gid=65534,
            config_file="/srv/jailer/run-abc/root/vmconfig.json",
        )
        # argv[0] is the jailer, not firecracker.
        assert argv[0].endswith("jailer")
        assert not argv[0].endswith("firecracker")

    def test_seccomp_level_2(self) -> None:
        argv = FirecrackerEngine().build_launch_argv(
            jailer_id="run-abc",
            chroot_base="/srv/jailer",
            uid=65534,
            gid=65534,
            config_file="/srv/jailer/run-abc/root/vmconfig.json",
        )
        assert "--seccomp-level" in argv
        idx = argv.index("--seccomp-level")
        assert argv[idx + 1] == "2"

    def test_drops_privileges_to_non_root(self) -> None:
        argv = FirecrackerEngine().build_launch_argv(
            jailer_id="run-abc",
            chroot_base="/srv/jailer",
            uid=65534,
            gid=65534,
            config_file="/srv/jailer/run-abc/root/vmconfig.json",
        )
        assert "--uid" in argv
        assert argv[argv.index("--uid") + 1] == "65534"
        assert "--gid" in argv
        assert argv[argv.index("--gid") + 1] == "65534"

    def test_firecracker_present_only_as_exec_file(self) -> None:
        argv = FirecrackerEngine().build_launch_argv(
            jailer_id="run-abc",
            chroot_base="/srv/jailer",
            uid=65534,
            gid=65534,
            config_file="/srv/jailer/run-abc/root/vmconfig.json",
        )
        assert "--exec-file" in argv
        exec_file = argv[argv.index("--exec-file") + 1]
        assert exec_file.endswith("firecracker")

    def test_engine_name_is_firecracker(self) -> None:
        assert FirecrackerEngine().name == "firecracker"


class TestVmBuiltinResolution:
    """REQ-001: vm resolves as a trusted built-in via load_backend, no manifest."""

    def test_load_backend_returns_vm(self) -> None:
        from arcrun.backends import load_backend

        backend = load_backend("vm", tier="federal")
        assert backend.name == "vm"
        assert backend.capabilities.isolation == "vm"

    def test_load_backend_vm_needs_no_manifest(self) -> None:
        from arcrun.backends import load_backend

        # No manifest_path supplied; built-ins are trusted at all tiers.
        backend = load_backend("vm", tier="personal")
        assert isinstance(backend, VmBackend)
