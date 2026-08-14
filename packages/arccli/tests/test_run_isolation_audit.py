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
from arctrust.paths import audit_dir, config_file, env_file

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


def _isolate_machine_config(monkeypatch: pytest.MonkeyPatch, arc_home: Path) -> None:
    """Point the whole arc home at an empty dir and clear env overrides.

    ``ARC_CONFIG_DIR`` is the ONLY isolation, deliberately: ``arc run`` resolves
    the machine config through ``arctrust.arc_home()``, so no module constant
    needs monkeypatching. A surface that still needed one would be a surface that
    reads the invoking user's real ``~/.arc/arcagent.toml`` on an isolated
    deployment — which is exactly the defect this replaces.
    """
    monkeypatch.setenv("ARC_CONFIG_DIR", str(arc_home))
    monkeypatch.delenv("ARC_TIER", raising=False)
    monkeypatch.delenv("ARC_RELAX_ISOLATION", raising=False)


def test_unconfigured_host_defaults_personal_local(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_machine_config(monkeypatch, tmp_path / "empty-home")
    assert run_cmd._machine_isolation() == ("personal", "local")


def test_enterprise_env_routes_to_container_no_local_relax(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_machine_config(monkeypatch, tmp_path / "empty-home")
    monkeypatch.setenv("ARC_TIER", "enterprise")
    # Enterprise floor is the container — relax must NOT default to host-local.
    assert run_cmd._machine_isolation() == ("enterprise", None)


def test_federal_env_routes_to_vm_no_local_relax(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_machine_config(monkeypatch, tmp_path / "empty-home")
    monkeypatch.setenv("ARC_TIER", "federal")
    assert run_cmd._machine_isolation() == ("federal", None)


def test_machine_config_tier_is_sourced(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    arc_home = tmp_path / "deployment"
    arc_home.mkdir()
    config_file("arcagent.toml", arc_home).parent.mkdir(parents=True)
    config_file("arcagent.toml", arc_home).write_text('[security]\ntier = "enterprise"\n')
    _isolate_machine_config(monkeypatch, arc_home)
    assert run_cmd._machine_isolation() == ("enterprise", None)


def test_machine_config_follows_arc_config_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An isolated deployment's tier must come from ITS arc home, not the user's.

    ``arc run`` used to read ``Path.home() / ".arc" / "arcagent.toml"``, frozen at
    import. On a box where the deployment declares federal and the invoking user's
    own home declares nothing, that resolved to "personal" — and personal defaults
    ``relax`` to host-local execution. The isolation floor was sourced from the
    wrong file, so a federal deployment ran agent code as a bare host subprocess.
    """
    federal_home = tmp_path / "federal-deployment"
    federal_home.mkdir()
    config_file("arcagent.toml", federal_home).parent.mkdir(parents=True)
    config_file("arcagent.toml", federal_home).write_text('[security]\ntier = "federal"\n')
    _isolate_machine_config(monkeypatch, federal_home)

    assert run_cmd._machine_isolation() == ("federal", None)

    # Same process, different deployment → different answer. A constant frozen at
    # import could not do this, which is what made the bug invisible.
    personal_home = tmp_path / "personal-deployment"
    personal_home.mkdir()
    _isolate_machine_config(monkeypatch, personal_home)
    assert run_cmd._machine_isolation() == ("personal", "local")


def test_direct_run_audit_lands_in_the_deployment_arc_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The direct-run audit trail follows the deployment, never the user's home.

    An audit file is the one thing that must not leak across deployments: records
    for an isolated deployment written into ``~/.arc/audit`` are both missing from
    the chain that should hold them and present in one that should not.
    """
    arc_home = tmp_path / "deployment"
    monkeypatch.setenv("ARC_CONFIG_DIR", str(arc_home))

    assert run_cmd._direct_run_audit_path() == audit_dir(arc_home) / "direct-run.jsonl"
    assert Path.home() not in run_cmd._direct_run_audit_path().parents


def test_arc_env_is_read_from_the_deployment_arc_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``arc.env`` follows the deployment too — it is where API keys live."""
    arc_home = tmp_path / "deployment"
    monkeypatch.setenv("ARC_CONFIG_DIR", str(arc_home))

    paths = run_cmd._env_paths()

    assert env_file(arc_home) in paths
    assert env_file(Path.home() / ".arc") not in paths
