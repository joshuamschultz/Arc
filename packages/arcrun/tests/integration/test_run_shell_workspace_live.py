"""Live container proof for run_shell — real Docker daemon only (REQ-021).

Double-guarded like the other live tests:
- @pytest.mark.slow so it is excluded from the fast unit run.
- @pytest.mark.skipif on the absence of the ``docker`` CLI.

Proves the enterprise container path:
1. writes made to /workspace appear on the HOST workspace (rw bind mount),
2. host paths outside the workspace are absent inside the sandbox,
3. host ``~/.arc`` secrets are NEVER mounted → unreadable from the sandbox.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from arcrun import run_shell

_HAS_DOCKER = shutil.which("docker") is not None

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not _HAS_DOCKER, reason="no docker CLI — container cannot run here"),
]


@pytest.mark.asyncio
async def test_workspace_write_lands_on_host(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "seed.txt").write_text("seed")

    raw = await run_shell(
        "echo hi > note.txt",
        tier="enterprise",
        workspace=ws,
        caller_did="did:arc:agent:live",
        timeout=60,
    )
    assert json.loads(raw)["exit_code"] == 0
    assert (ws / "note.txt").read_text().strip() == "hi"


@pytest.mark.asyncio
async def test_host_path_outside_workspace_absent(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    outside = tmp_path / "outside_secret.txt"
    outside.write_text("top secret")

    raw = await run_shell(
        f"cat {outside}",
        tier="enterprise",
        workspace=ws,
        timeout=60,
    )
    result = json.loads(raw)
    assert result["exit_code"] != 0
    assert "top secret" not in result["stdout"]


@pytest.mark.asyncio
async def test_host_operator_key_never_mounted(tmp_path: Path, monkeypatch) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    # A fake operator key OUTSIDE the workspace, under a fake HOME's ~/.arc.
    fake_home = tmp_path / "home"
    key = fake_home / ".arc" / "operator" / "operator.key"
    key.parent.mkdir(parents=True)
    key.write_text("PRIVATE-KEY-MATERIAL")
    monkeypatch.setenv("HOME", str(fake_home))

    raw = await run_shell(
        "cat ~/.arc/operator/operator.key",
        tier="enterprise",
        workspace=ws,
        timeout=60,
    )
    result = json.loads(raw)
    assert result["exit_code"] != 0
    assert "PRIVATE-KEY-MATERIAL" not in result["stdout"]
