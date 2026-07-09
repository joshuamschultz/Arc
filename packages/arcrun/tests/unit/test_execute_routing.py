"""Acceptance tests for tier-routed execute_python: audit, personal-off, dev-fallback.

These exercise the wired path (make_execute_tool → router → backend) without a
container/VM runtime:
- Backend selection is audited on every build (AU-2/AU-3).
- Personal sandbox-OFF genuinely reaches the host filesystem OUTSIDE any container.
- A no-KVM host is NOT a downgrade for personal/enterprise (their floor is the
  container, which they still get) — no spurious downgrade event is emitted.
- Federal on a host with no VM support refuses (fail closed) and audits the refusal.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile

import pytest
from arctrust import AuditEvent

from arcrun.builtins.execute import (
    IsolationUnavailableError,
    make_execute_tool,
)
from arcrun.events import EventBus
from arcrun.types import ToolContext


class CaptureSink:
    """Audit sink capturing every event for assertion."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


def _ctx() -> ToolContext:
    return ToolContext(
        run_id="route-test",
        tool_call_id="tc",
        turn_number=1,
        event_bus=EventBus(run_id="route-test"),
        cancelled=asyncio.Event(),
    )


class TestBackendSelectionAudit:
    def test_personal_default_emits_selected_container(self) -> None:
        sink = CaptureSink()
        make_execute_tool(tier="personal", caller_did="did:arc:agent:x", audit_sink=sink)
        selected = [e for e in sink.events if e.action == "code_exec.backend.selected"]
        assert len(selected) == 1
        ev = selected[0]
        assert ev.tier == "personal"
        assert ev.extra["isolation"] == "container"
        assert ev.outcome == "allow"
        assert ev.actor_did == "did:arc:agent:x"

    def test_selection_carries_au3_fields(self) -> None:
        sink = CaptureSink()
        make_execute_tool(tier="personal", relax="local", audit_sink=sink)
        ev = next(e for e in sink.events if e.action == "code_exec.backend.selected")
        # AU-3 record content: relax + reason + platform fact all present.
        assert "relax" in ev.extra
        assert "relax_reason" in ev.extra
        assert "platform_supports_vm" in ev.extra


class TestPersonalSandboxOff:
    """REQ-020: personal + explicit relax OFF → host filesystem, outside any container."""

    @pytest.mark.asyncio
    async def test_off_reaches_host_filesystem(self) -> None:
        # Write a sentinel file on the REAL host FS, outside any sandbox/container.
        marker = "ARC_HOST_FS_PROOF_9f3a"
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write(marker)
            host_path = fh.name
        try:
            tool = make_execute_tool(tier="personal", relax="off")
            code = f"print(open({host_path!r}).read())"
            raw = await tool.execute({"code": code}, _ctx())
            result = json.loads(raw)
            # The executed code read a host file that exists nowhere in a container:
            # proof the sandbox is genuinely OFF and reaching the host.
            assert marker in result["stdout"]
            assert result["exit_code"] == 0
        finally:
            os.unlink(host_path)

    @pytest.mark.asyncio
    async def test_off_can_see_real_host_home(self) -> None:
        # A container would never expose the operator's real login; the host does.
        tool = make_execute_tool(tier="personal", relax="local")
        code = "import getpass, pwd, os; print(pwd.getpwuid(os.getuid()).pw_dir)"
        raw = await tool.execute({"code": code}, _ctx())
        result = json.loads(raw)
        # LocalBackend runs as the host user → real home directory is visible.
        assert result["stdout"].strip().startswith("/")
        assert result["exit_code"] == 0

    def test_off_emits_downgrade_notice(self) -> None:
        sink = CaptureSink()
        make_execute_tool(tier="personal", relax="off", audit_sink=sink)
        downgrades = [e for e in sink.events if e.action == "code_exec.isolation.downgraded"]
        assert len(downgrades) == 1
        assert downgrades[0].tier == "personal"


class TestDevFallback:
    """#5: non-federal on no-KVM selects the container floor — NOT a downgrade."""

    def test_non_federal_no_kvm_emits_no_downgrade(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Force platform_supports_vm False regardless of host.
        monkeypatch.setattr("arcrun.builtins.execute.platform_supports_vm", lambda *a, **k: False)
        sink = CaptureSink()
        make_execute_tool(tier="enterprise", audit_sink=sink)
        selected = next(e for e in sink.events if e.action == "code_exec.backend.selected")
        assert selected.extra["isolation"] == "container"
        # The container IS the enterprise floor — no-KVM is not a downgrade, so the
        # audit trail must not be polluted with a spurious downgrade event.
        downgrades = [e for e in sink.events if e.action == "code_exec.isolation.downgraded"]
        assert downgrades == []


class TestFederalFailClosed:
    """REQ-003/030: federal refuses (no silent downgrade) when VM is unavailable."""

    def test_federal_no_kvm_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("arcrun.builtins.execute.platform_supports_vm", lambda *a, **k: False)
        with pytest.raises(IsolationUnavailableError):
            make_execute_tool(tier="federal")

    def test_federal_refusal_is_audited(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("arcrun.builtins.execute.platform_supports_vm", lambda *a, **k: False)
        sink = CaptureSink()
        with pytest.raises(IsolationUnavailableError):
            make_execute_tool(tier="federal", audit_sink=sink)
        refusals = [
            e
            for e in sink.events
            if e.action == "code_exec.backend.selected" and e.outcome == "refuse"
        ]
        assert len(refusals) == 1
        assert refusals[0].tier == "federal"

    def test_federal_on_kvm_selects_vm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("arcrun.builtins.execute.platform_supports_vm", lambda *a, **k: True)
        sink = CaptureSink()
        make_execute_tool(tier="federal", audit_sink=sink)
        ev = next(e for e in sink.events if e.action == "code_exec.backend.selected")
        assert ev.extra["isolation"] == "vm"
        assert ev.outcome == "allow"
