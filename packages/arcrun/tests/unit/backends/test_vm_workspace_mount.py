"""C2 — VmBackend guest workspace share parity (host-independent).

Firecracker cannot boot here, so this asserts the workspace share is encoded into
the rendered machine config: workspace read-write at ``/workspace``, host ``~/.arc``
absent, and each protected subpath marked read-only. Mirrors the container backend's
posture so the federal (VM) path enforces the same REQ-021 boundary.
"""

from __future__ import annotations

import json
from pathlib import Path

from arcrun.backends.vm import VmBackend


def test_workspace_share_rendered_into_vmconfig(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "identity.md").write_text("did:arc:agent:beta")

    backend = VmBackend(
        chroot_base=str(tmp_path / "jail"),
        workspace_mount=ws,
        readonly_subpaths=[Path("identity.md"), Path("policy.md")],
    )
    config_path = backend._write_vmconfig("arc-vm-ws", "echo hi", cwd="/workspace", env=None)
    data = json.loads(Path(config_path).read_text())

    share = data["workspace"]
    assert share["host_path"] == str(ws)
    assert share["guest_path"] == "/workspace"
    assert share["read_only"] is False
    # identity.md exists → protected read-only; policy.md absent → not listed.
    assert "identity.md" in share["readonly_subpaths"]
    assert "policy.md" not in share["readonly_subpaths"]


def test_no_workspace_leaves_config_without_share(tmp_path: Path) -> None:
    backend = VmBackend(chroot_base=str(tmp_path / "jail"))
    config_path = backend._write_vmconfig("arc-vm-plain", "echo hi", cwd=None, env=None)
    data = json.loads(Path(config_path).read_text())
    assert "workspace" not in data


def test_workspace_flips_bind_mount_capability(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    assert VmBackend(workspace_mount=ws).capabilities.supports_bind_mount is True
    # Default (no workspace) keeps the container-only posture.
    assert VmBackend().capabilities.supports_bind_mount is False
