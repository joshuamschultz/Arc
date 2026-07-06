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

import json
from pathlib import Path

import pytest

from arcrun.backends import ExecutorBackend
from arcrun.backends.vm import (
    FirecrackerEngine,
    VmBackend,
    VmGuestPolicy,
    VmUnavailableError,
)


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


class TestVmGuestPostureConfig:
    """#4: vmconfig.json is rendered from self._policy and written to the chroot."""

    def test_vmconfig_rendered_from_policy(self, tmp_path: Path) -> None:
        policy = VmGuestPolicy(
            network=False,
            read_only_rootfs=True,
            vcpu_count=2,
            mem_limit_mib=128,
            pids_limit=32,
        )
        backend = VmBackend(chroot_base=str(tmp_path), policy=policy)
        config_path = backend._write_vmconfig(
            "arc-vm-abc123", "python3 -", cwd="/tmp", env={"A": "b"}
        )
        data = json.loads(Path(config_path).read_text())

        # Machine caps come straight from the policy.
        assert data["machine-config"]["vcpu_count"] == 2
        assert data["machine-config"]["mem_size_mib"] == 128
        # Deny-by-default egress → no NICs; rootfs is read-only.
        assert data["network-interfaces"] == []
        assert data["drives"][0]["is_read_only"] is True
        # Config lives under the SAME jailer id it was rendered for.
        assert "arc-vm-abc123" in config_path

    def test_network_enabled_policy_adds_nic(self, tmp_path: Path) -> None:
        backend = VmBackend(
            chroot_base=str(tmp_path), policy=VmGuestPolicy(network=True)
        )
        config_path = backend._write_vmconfig("arc-vm-net", "python3 -", cwd=None, env=None)
        data = json.loads(Path(config_path).read_text())
        assert data["network-interfaces"] != []


class TestVmSingleJailerId:
    """#4: the jailer --id and the rendered config path MUST reference one id."""

    @pytest.mark.asyncio
    async def test_run_separated_uses_single_jailer_id(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        backend = VmBackend(chroot_base=str(tmp_path))
        # Bypass the KVM gate so the launch path executes on this non-KVM host.
        monkeypatch.setattr(backend, "_ensure_available", lambda: None)

        captured: dict[str, str] = {}

        class _RecordingEngine:
            name = "recording"

            def build_launch_argv(
                self,
                *,
                jailer_id: str,
                chroot_base: str,
                uid: int,
                gid: int,
                config_file: str,
            ) -> list[str]:
                captured["jailer_id"] = jailer_id
                captured["config_file"] = config_file
                return ["true"]

        backend._engine = _RecordingEngine()

        class _FakeProc:
            returncode = 0

            async def communicate(self) -> tuple[bytes, bytes]:
                return b"", b""

        async def fake_exec(*argv: str, **kwargs: object) -> _FakeProc:
            return _FakeProc()

        monkeypatch.setattr("arcrun.backends.vm.asyncio.create_subprocess_exec", fake_exec)

        await backend.run_separated("python3 -", stdin="print(1)")

        # The id passed to the jailer --id and the id in the config path agree.
        assert captured["jailer_id"] in captured["config_file"]
        assert Path(captured["config_file"]).exists()


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
