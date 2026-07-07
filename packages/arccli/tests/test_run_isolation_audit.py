"""#2/#3 — `arc run` sources tier from machine config and audits backend selection.

- #2: the direct-run path threads the caller DID + an audit sink into
  make_execute_tool, so the backend-selection AuditEvent is PERSISTED and
  attributed (previously every call site passed neither → logger-only, no DID).
- #3: tier is sourced from ARC_TIER / ~/.arc/arcagent.toml [security].tier and
  defaults to personal (host dev tool) ONLY when genuinely unconfigured; a
  configured enterprise/federal host is NOT silently run unsandboxed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from arctrust import AuditEvent

from arccli.commands import run as run_cmd


class CaptureSink:
    """Audit sink capturing every event for assertion."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


# ---------------------------------------------------------------------------
# #2 — selection audit reaches an injected sink with the caller DID
# ---------------------------------------------------------------------------


def test_exec_selection_audit_reaches_sink_with_caller_did() -> None:
    sink = CaptureSink()
    did = "did:arc:operator:cli-9f3a"
    # personal + local keeps the execution on the host (no docker/KVM needed).
    asyncio.run(
        run_cmd._run_exec_async(
            "print('hi')",
            30.0,
            65536,
            True,
            "personal",
            "local",
            did,
            sink,
        )
    )
    selected = [e for e in sink.events if e.action == "code_exec.backend.selected"]
    assert len(selected) == 1
    assert selected[0].actor_did == did
    assert selected[0].outcome == "allow"


# ---------------------------------------------------------------------------
# #3 — tier is sourced from machine config, not hardcoded
# ---------------------------------------------------------------------------


def _isolate_machine_config(monkeypatch: pytest.MonkeyPatch, missing: Path) -> None:
    """Point the machine-config path at a nonexistent file and clear env overrides."""
    monkeypatch.setattr(run_cmd, "_MACHINE_CONFIG", missing)
    monkeypatch.delenv("ARC_TIER", raising=False)
    monkeypatch.delenv("ARC_RELAX_ISOLATION", raising=False)


def test_unconfigured_host_defaults_personal_local(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_machine_config(monkeypatch, tmp_path / "absent.toml")
    assert run_cmd._machine_isolation() == ("personal", "local")


def test_enterprise_env_routes_to_container_no_local_relax(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_machine_config(monkeypatch, tmp_path / "absent.toml")
    monkeypatch.setenv("ARC_TIER", "enterprise")
    # Enterprise floor is the container — relax must NOT default to host-local.
    assert run_cmd._machine_isolation() == ("enterprise", None)


def test_federal_env_routes_to_vm_no_local_relax(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_machine_config(monkeypatch, tmp_path / "absent.toml")
    monkeypatch.setenv("ARC_TIER", "federal")
    assert run_cmd._machine_isolation() == ("federal", None)


def test_machine_config_tier_is_sourced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = tmp_path / "arcagent.toml"
    cfg.write_text('[security]\ntier = "enterprise"\n')
    monkeypatch.setattr(run_cmd, "_MACHINE_CONFIG", cfg)
    monkeypatch.delenv("ARC_TIER", raising=False)
    monkeypatch.delenv("ARC_RELAX_ISOLATION", raising=False)
    assert run_cmd._machine_isolation() == ("enterprise", None)
