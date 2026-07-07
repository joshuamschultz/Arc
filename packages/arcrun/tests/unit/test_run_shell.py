"""C3 — run_shell: tier-routed shell execution through the isolation backend.

These cover the host-independent surface: the fail-closed federal refusal, the
``code_exec.backend.selected`` audit emission (reusing SPEC-036's event), and that
the backend is constructed WITH the workspace mount + protected subpaths. The
real-container proof (files land in the host workspace, ~/.arc is absent) lives in
tests/integration/test_run_shell_workspace_live.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from arctrust import AuditEvent

from arcrun import run_shell
from arcrun.backends.base import SeparatedResult
from arcrun.builtins.execute import IsolationUnavailableError


class CaptureSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_federal_without_vm_fails_closed(tmp_path: Path) -> None:
    sink = CaptureSink()
    with pytest.raises(IsolationUnavailableError):
        await run_shell(
            "echo hi",
            tier="federal",
            workspace=tmp_path,
            caller_did="did:arc:agent:x",
            audit_sink=sink,
            platform_supports_vm=False,
        )
    # The refusal is audited too (outcome=refuse).
    selected = [e for e in sink.events if e.action == "code_exec.backend.selected"]
    assert selected and selected[0].outcome == "refuse"


@pytest.mark.asyncio
async def test_emits_backend_selected_audit(tmp_path: Path) -> None:
    sink = CaptureSink()
    # personal + relax=off → local backend; runs on host so no docker needed.
    raw = await run_shell(
        "echo audit_ok",
        tier="personal",
        workspace=tmp_path,
        caller_did="did:arc:agent:y",
        audit_sink=sink,
        relax="off",
    )
    result = json.loads(raw)
    assert "audit_ok" in result["stdout"]
    assert result["exit_code"] == 0

    selected = [e for e in sink.events if e.action == "code_exec.backend.selected"]
    assert selected
    assert selected[0].tier == "personal"
    assert selected[0].target == "local"
    assert selected[0].outcome == "allow"


@pytest.mark.asyncio
async def test_constructs_backend_with_workspace_mount(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}

    class _SpyBackend:
        def __init__(
            self,
            *,
            workspace_mount: Path,
            readonly_subpaths: list[Path] | None,
            max_stdout_bytes: int,
        ) -> None:
            captured["workspace_mount"] = workspace_mount
            captured["readonly_subpaths"] = readonly_subpaths

        async def run_separated(
            self, command: str, *, cwd: str | None = None, **kwargs: Any
        ) -> SeparatedResult:
            captured["command"] = command
            captured["cwd"] = cwd
            return SeparatedResult(stdout=b"ok", stderr=b"", exit_code=0)

        async def close(self) -> None:
            pass

    monkeypatch.setattr("arcrun.builtins.execute.DockerBackend", _SpyBackend)

    subs = [Path("identity.md")]
    raw = await run_shell(
        "ls",
        tier="enterprise",
        workspace=tmp_path,
        readonly_subpaths=subs,
        platform_supports_vm=False,
    )
    assert json.loads(raw)["exit_code"] == 0
    assert captured["workspace_mount"] == tmp_path
    assert captured["readonly_subpaths"] == subs
    assert captured["command"] == "ls"
    assert captured["cwd"] == "/workspace"
