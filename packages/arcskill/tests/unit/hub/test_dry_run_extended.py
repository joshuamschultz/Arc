"""Extended tests for arcskill.hub.dry_run — covering uncovered branches.

Targets:
- run_dry_run: skip_sandbox path
- _find_fixture_command: MODULE.yaml present with test_fixture, skill.py fallback,
  __init__.py fallback, main.py fallback, no Python file fallback,
  MODULE.yaml parse failure fallback, MODULE.yaml missing test_fixture key
- _safe_extract: path-traversal rejection (both absolute path and .. in name)
- _docker_available: True/False branches
- _run_in_sandbox: no-firecracker + docker-available path, no-firecracker + no-docker
- FirecrackerConfig: JSON payload helpers
- FirecrackerSandbox.build_jailer_config: with and without network interface
- _platform_is_macos: both branches
"""

from __future__ import annotations

import io
import tarfile
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from arcskill.hub.config import HubConfig, TierPolicy
from arcskill.hub.dry_run import (
    DryRunResult,
    FirecrackerConfig,
    FirecrackerSandbox,
    _docker_available,
    _find_fixture_command,
    _platform_is_macos,
    _run_docker,
    _run_firecracker,
    _safe_extract,
    run_dry_run,
)
from arcskill.hub.errors import SandboxRequired

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _personal_config() -> HubConfig:
    return HubConfig(enabled=True, tier=TierPolicy(level="personal"))


def _federal_config() -> HubConfig:
    return HubConfig(enabled=True, tier=TierPolicy(level="federal"))


def _make_tarball(files: dict[str, str], *, with_traversal: bool = False) -> Path:
    """Create a .tar.gz with the given filename → content pairs.

    If *with_traversal* is True, also add a malicious entry with a '../' path.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="arcskill_dr_"))
    bundle = tmpdir / "skill.tar.gz"

    with tarfile.open(bundle, "w:gz") as tf:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))

        if with_traversal:
            data = b"evil"
            info = tarfile.TarInfo(name="../evil.py")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))

    return bundle


# ---------------------------------------------------------------------------
# run_dry_run — skip_sandbox path
# ---------------------------------------------------------------------------


def test_run_dry_run_skip_sandbox_raises_at_personal() -> None:
    """skip_sandbox=True + non-federal → SandboxRequired (bypass removed).

    Tier controls which backend runs, not whether to sandbox.
    """
    bundle = _make_tarball({"skill.py": "# skill\n"})
    config = _personal_config()

    with pytest.raises(SandboxRequired, match="skip_sandbox=True is not permitted"):
        run_dry_run(bundle, config, skip_sandbox=True)


def test_run_dry_run_skip_sandbox_raises_at_federal() -> None:
    """skip_sandbox=True raises SandboxRequired at federal tier too."""
    bundle = _make_tarball({"skill.py": "# skill\n"})
    config = _federal_config()

    with pytest.raises(SandboxRequired):
        run_dry_run(bundle, config, skip_sandbox=True)


# ---------------------------------------------------------------------------
# _find_fixture_command — all branches
# ---------------------------------------------------------------------------


def test_find_fixture_command_reads_module_yaml() -> None:
    """Returns the test_fixture from MODULE.yaml when present."""
    with tempfile.TemporaryDirectory() as tmpdir_str:
        skill_dir = Path(tmpdir_str)
        module_yaml = skill_dir / "MODULE.yaml"
        module_yaml.write_text("test_fixture: pytest tests/ -x\n", encoding="utf-8")

        result = _find_fixture_command(skill_dir)

    assert result == "pytest tests/ -x"


def test_find_fixture_command_module_yaml_no_test_fixture_key() -> None:
    """MODULE.yaml exists but has no test_fixture → falls back to file detection."""
    with tempfile.TemporaryDirectory() as tmpdir_str:
        skill_dir = Path(tmpdir_str)
        (skill_dir / "MODULE.yaml").write_text("name: my-skill\n", encoding="utf-8")
        (skill_dir / "skill.py").write_text("# skill\n", encoding="utf-8")

        result = _find_fixture_command(skill_dir)

    assert "skill" in result
    assert "import" in result


def test_find_fixture_command_module_yaml_parse_failure_falls_back() -> None:
    """MODULE.yaml that fails to parse is silently skipped; file fallback used."""
    with tempfile.TemporaryDirectory() as tmpdir_str:
        skill_dir = Path(tmpdir_str)
        # Write invalid YAML that will trigger parse error
        (skill_dir / "MODULE.yaml").write_text(":\tbad: yaml: :\n", encoding="utf-8")
        (skill_dir / "__init__.py").write_text("# init\n", encoding="utf-8")

        result = _find_fixture_command(skill_dir)

    # Falls back to __init__.py detection
    assert "import" in result


def test_find_fixture_command_skill_py_fallback() -> None:
    """No MODULE.yaml; skill.py present → python -c "import skill" fallback."""
    with tempfile.TemporaryDirectory() as tmpdir_str:
        skill_dir = Path(tmpdir_str)
        (skill_dir / "skill.py").write_text("# skill\n", encoding="utf-8")

        result = _find_fixture_command(skill_dir)

    assert "import skill" in result


def test_find_fixture_command_init_py_fallback() -> None:
    """No MODULE.yaml; __init__.py present → python -c "import skill" fallback."""
    with tempfile.TemporaryDirectory() as tmpdir_str:
        skill_dir = Path(tmpdir_str)
        (skill_dir / "__init__.py").write_text("# init\n", encoding="utf-8")

        result = _find_fixture_command(skill_dir)

    assert "import skill" in result


def test_find_fixture_command_main_py_fallback() -> None:
    """No MODULE.yaml; main.py present → python -c "import main" fallback."""
    with tempfile.TemporaryDirectory() as tmpdir_str:
        skill_dir = Path(tmpdir_str)
        (skill_dir / "main.py").write_text("# main\n", encoding="utf-8")

        result = _find_fixture_command(skill_dir)

    assert "import main" in result


def test_find_fixture_command_no_files_default() -> None:
    """No MODULE.yaml, no Python files → default print command."""
    with tempfile.TemporaryDirectory() as tmpdir_str:
        skill_dir = Path(tmpdir_str)

        result = _find_fixture_command(skill_dir)

    assert "dry-run ok" in result


# ---------------------------------------------------------------------------
# _safe_extract — path-traversal rejection
# ---------------------------------------------------------------------------


def test_safe_extract_rejects_absolute_paths() -> None:
    """Tarball entries with absolute paths are skipped (not extracted)."""
    # Build tarball including an absolute-path entry from the start
    tmpdir = Path(tempfile.mkdtemp(prefix="arcskill_dr_"))
    bundle = tmpdir / "skill.tar.gz"

    with tarfile.open(bundle, "w:gz") as tf:
        # Safe entry
        data = b"# ok\n"
        info = tarfile.TarInfo(name="normal.py")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))

        # Malicious absolute-path entry
        evil = b"evil"
        evil_info = tarfile.TarInfo(name="/etc/evil.py")
        evil_info.size = len(evil)
        tf.addfile(evil_info, io.BytesIO(evil))

    with tempfile.TemporaryDirectory() as tmpdir_str:
        dest = Path(tmpdir_str) / "extracted"
        dest.mkdir()
        _safe_extract(bundle, dest)

        # normal.py extracted, /etc/evil.py skipped
        assert (dest / "normal.py").exists()
        # The absolute path should not create a file inside dest
        assert not (dest / "etc" / "evil.py").exists()


def test_safe_extract_rejects_dotdot_traversal() -> None:
    """Tarball entries with .. in name are skipped."""
    bundle = _make_tarball({"safe.py": "# safe\n"}, with_traversal=True)

    with tempfile.TemporaryDirectory() as tmpdir_str:
        dest = Path(tmpdir_str) / "extracted"
        dest.mkdir()
        _safe_extract(bundle, dest)

        assert (dest / "safe.py").exists()
        # The traversal file must not appear
        parent_evil = Path(tmpdir_str) / "evil.py"
        assert not parent_evil.exists()


def test_safe_extract_normal_bundle_extracts_all() -> None:
    """Normal tarball (no traversal entries) extracts all files."""
    bundle = _make_tarball({"a.py": "# a\n", "b.py": "# b\n"})

    with tempfile.TemporaryDirectory() as tmpdir_str:
        dest = Path(tmpdir_str) / "extracted"
        dest.mkdir()
        _safe_extract(bundle, dest)

        assert (dest / "a.py").exists()
        assert (dest / "b.py").exists()


# ---------------------------------------------------------------------------
# _docker_available
# ---------------------------------------------------------------------------


def test_docker_available_returns_true_when_on_path() -> None:
    with patch("arcskill.hub.dry_run.shutil.which", return_value="/usr/bin/docker"):
        assert _docker_available() is True


def test_docker_available_returns_false_when_absent() -> None:
    with patch("arcskill.hub.dry_run.shutil.which", return_value=None):
        assert _docker_available() is False


# ---------------------------------------------------------------------------
# _platform_is_macos
# ---------------------------------------------------------------------------


def test_platform_is_macos_returns_true_on_darwin() -> None:
    with patch("arcskill.hub.dry_run.platform.system", return_value="Darwin"):
        assert _platform_is_macos() is True


def test_platform_is_macos_returns_false_on_linux() -> None:
    with patch("arcskill.hub.dry_run.platform.system", return_value="Linux"):
        assert _platform_is_macos() is False


# ---------------------------------------------------------------------------
# FirecrackerConfig — JSON payload helpers
# ---------------------------------------------------------------------------


def test_firecracker_config_machine_config_json() -> None:
    """to_machine_config_json returns correct vcpu/mem fields."""
    cfg = FirecrackerConfig(
        kernel_path=Path("/vmlinux.bin"),
        rootfs_path=Path("/rootfs.ext4"),
        jailer_binary=Path("/usr/bin/jailer"),
        vcpu_count=2,
        mem_size_mib=256,
    )
    mc = cfg.to_machine_config_json()
    assert mc["vcpu_count"] == 2
    assert mc["mem_size_mib"] == 256


def test_firecracker_config_boot_source_json() -> None:
    """to_boot_source_json includes kernel_image_path."""
    cfg = FirecrackerConfig(
        kernel_path=Path("/vmlinux.bin"),
        rootfs_path=Path("/rootfs.ext4"),
        jailer_binary=Path("/usr/bin/jailer"),
    )
    bs = cfg.to_boot_source_json()
    assert bs["kernel_image_path"] == "/vmlinux.bin"
    assert "boot_args" in bs


def test_firecracker_config_rootfs_drive_json() -> None:
    """to_rootfs_drive_json marks drive as root and read-only."""
    cfg = FirecrackerConfig(
        kernel_path=Path("/vmlinux.bin"),
        rootfs_path=Path("/rootfs.ext4"),
        jailer_binary=Path("/usr/bin/jailer"),
    )
    rd = cfg.to_rootfs_drive_json()
    assert rd["is_root_device"] is True
    assert rd["is_read_only"] is True
    assert rd["path_on_host"] == "/rootfs.ext4"


# ---------------------------------------------------------------------------
# FirecrackerSandbox.build_jailer_config
# ---------------------------------------------------------------------------


def test_build_jailer_config_no_network_omits_network_interfaces() -> None:
    """When network_interface='none', the config has no network-interfaces key."""
    sandbox = FirecrackerSandbox(
        kernel_path=Path("/vmlinux.bin"),
        rootfs_path=Path("/rootfs.ext4"),
        jailer_binary=Path("/usr/bin/jailer"),
        network_interface="none",
    )
    cfg = sandbox.build_jailer_config("test-vm-id")
    assert "network-interfaces" not in cfg
    assert cfg["machine-config"]["vcpu_count"] == 1


def test_build_jailer_config_with_network_includes_interface() -> None:
    """When network_interface is set, the config includes network-interfaces."""
    sandbox = FirecrackerSandbox(
        kernel_path=Path("/vmlinux.bin"),
        rootfs_path=Path("/rootfs.ext4"),
        jailer_binary=Path("/usr/bin/jailer"),
        network_interface="eth0",
    )
    cfg = sandbox.build_jailer_config("test-vm-id")
    assert "network-interfaces" in cfg
    assert cfg["network-interfaces"][0]["host_dev_name"] == "eth0"


def test_build_jailer_config_vsock_uses_vm_id() -> None:
    """The vsock UDS path embeds the vm_id."""
    sandbox = FirecrackerSandbox(
        kernel_path=Path("/vmlinux.bin"),
        rootfs_path=Path("/rootfs.ext4"),
        jailer_binary=Path("/usr/bin/jailer"),
    )
    vm_id = "abc-123"
    cfg = sandbox.build_jailer_config(vm_id)
    assert vm_id in cfg["vsock"]["uds_path"]


# ---------------------------------------------------------------------------
# _run_in_sandbox — fallback paths (no firecracker)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_in_sandbox_docker_fallback_when_available() -> None:
    """When Firecracker unavailable but Docker is, Docker path is attempted."""
    from arcskill.hub.dry_run import _run_in_sandbox

    skill_dir = Path(tempfile.mkdtemp())
    config = _personal_config()

    expected = DryRunResult(passed=True, backend_used="docker", exit_code=0)

    with patch("arcskill.hub.dry_run.is_firecracker_available", return_value=False):
        with patch("arcskill.hub.dry_run._docker_available", return_value=True):
            with patch("arcskill.hub.dry_run._run_docker", new_callable=AsyncMock) as mock_docker:
                mock_docker.return_value = expected
                result = await _run_in_sandbox("pytest", skill_dir, config)

    assert result.backend_used == "docker"
    mock_docker.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_in_sandbox_skipped_when_no_docker_no_firecracker() -> None:
    """Non-federal, no Firecracker, no Docker → skipped result with audit warning."""
    from arcskill.hub.dry_run import _run_in_sandbox

    skill_dir = Path(tempfile.mkdtemp())
    config = _personal_config()

    with patch("arcskill.hub.dry_run.is_firecracker_available", return_value=False):
        with patch("arcskill.hub.dry_run._docker_available", return_value=False):
            result = await _run_in_sandbox("pytest", skill_dir, config)

    assert result.skipped is True
    assert result.backend_used == "skipped"


@pytest.mark.asyncio
async def test_run_in_sandbox_federal_raises_when_no_firecracker() -> None:
    """Federal tier: no Firecracker → SandboxRequired."""
    from arcskill.hub.dry_run import _run_in_sandbox

    skill_dir = Path(tempfile.mkdtemp())
    config = _federal_config()

    with patch("arcskill.hub.dry_run.is_firecracker_available", return_value=False):
        with pytest.raises(SandboxRequired, match="Federal tier requires Firecracker"):
            await _run_in_sandbox("pytest", skill_dir, config)


# ---------------------------------------------------------------------------
# _run_firecracker — missing kernel/rootfs raises SandboxRequired
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_firecracker_raises_when_kernel_missing() -> None:
    """_run_firecracker raises SandboxRequired when kernel image is absent."""
    skill_dir = Path(tempfile.mkdtemp())

    # Ensure neither path exists by pointing to non-existent files
    with patch.dict(
        "os.environ",
        {
            "ARC_FC_KERNEL": "/nonexistent/vmlinux.bin",
            "ARC_FC_ROOTFS": "/nonexistent/rootfs.ext4",
        },
    ):
        with pytest.raises(SandboxRequired, match="kernel"):
            await _run_firecracker("pytest", skill_dir)


# ---------------------------------------------------------------------------
# _run_docker — DockerBackend None path (arcrun not installed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_docker_returns_skipped_when_no_docker_backend() -> None:
    """_run_docker returns skipped result when DockerBackend is None."""
    skill_dir = Path(tempfile.mkdtemp())

    with patch("arcskill.hub.dry_run._DockerBackend", None):
        result = await _run_docker("pytest", skill_dir)

    assert result.skipped is True
    assert result.backend_used == "skipped"


@pytest.mark.asyncio
async def test_run_docker_success_with_mock_backend() -> None:
    """_run_docker returns passed result when backend runs successfully."""

    skill_dir = Path(tempfile.mkdtemp())

    async def _fake_stream(_handle: object) -> AsyncIterator[bytes]:
        yield b"all tests passed\n"

    mock_backend = AsyncMock()
    mock_backend.run = AsyncMock(return_value="handle-123")
    mock_backend.stream = _fake_stream
    mock_backend.close = AsyncMock()

    mock_backend_cls = MagicMock(return_value=mock_backend)

    with patch("arcskill.hub.dry_run._DockerBackend", mock_backend_cls):
        result = await _run_docker("pytest", skill_dir)

    assert result.passed is True
    assert result.backend_used == "docker"
    assert result.exit_code == 0


@pytest.mark.asyncio
async def test_run_docker_timeout_sets_exit_code_minus_one() -> None:
    """_run_docker TimeoutError sets exit_code=-1 and passed=False."""
    skill_dir = Path(tempfile.mkdtemp())

    mock_backend = AsyncMock()
    mock_backend.run = AsyncMock(side_effect=TimeoutError("timed out"))
    mock_backend.close = AsyncMock()

    mock_backend_cls = MagicMock(return_value=mock_backend)

    with patch("arcskill.hub.dry_run._DockerBackend", mock_backend_cls):
        result = await _run_docker("pytest", skill_dir)

    assert result.passed is False
    assert result.exit_code == -1
    assert result.backend_used == "docker"
