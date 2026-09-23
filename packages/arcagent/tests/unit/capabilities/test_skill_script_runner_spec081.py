"""SPEC-081 Phase 2 COMP-005 — tiered skill-script runner (RED wave).

Pins the contract of ``arcagent.capabilities.skill_script_runner``: a runner that
resolves a promoted skill's own folder as the ONLY sandbox root, verifies the
requested script resolves inside that folder, routes execution to arcrun's
tier-selected isolation backend, emits a code-exec backend-selected audit event,
and returns the script's output as TYPED tool-result data (never a string spliced
into instructions).

The arcrun execution seam is mocked at the boundary — these tests require neither
Docker nor Firecracker. ``resolve_execution_backend`` is delegated to the REAL
pure router so tier-routing and federal fail-closed behaviour are exercised for
real, not asserted against a mock's opinion.

Test → task/requirement map:
  T-1062 (REQ-404, REQ-408) personal: typed result, skill-folder mount, python3,
      backend-selected audit event, backend name on result.
  T-1064 (REQ-406, REQ-407) enterprise: container jail to the skill folder only,
      no parent/agent-workspace path, traversal rejected before exec; plus an
      OPTIONAL real-Docker abuse case (socket + out-of-folder read) skipped when
      no daemon is reachable.
  T-1066 (REQ-405) federal: VM backend floor; fail closed (never downgrade) when
      the platform has no VM support.
  General: a non-.py script is refused (Python-only in this phase).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import arcrun
import pytest

# arcrun's fail-closed error for a federal tier with no VM support.
from arcrun.builtins.execute import IsolationUnavailableError
from arctrust.identity import AgentIdentity

from arcagent.capabilities import artifact_signing

# NOTE: This import is EXPECTED to fail (ModuleNotFoundError) in the RED wave —
# the module does not exist yet. That is the correct "feature absent" signal.
# The names below define the interface the GREEN coder must implement.
from arcagent.capabilities.skill_script_runner import (
    ScriptPathError,
    SkillScriptError,
    SkillScriptResult,
    SkillScriptRunner,
    UnsupportedScriptError,
)

# --------------------------------------------------------------------------- #
# Test doubles — a recording audit sink and a boundary-faithful arcrun mock.
# --------------------------------------------------------------------------- #


class _SpySink:
    """arctrust AuditSink: any object with ``write(event) -> None``."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def write(self, event: Any) -> None:
        self.events.append(event)


def _make_fake_run_shell(
    captured: dict[str, Any],
    *,
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
) -> Any:
    """Fake ``arcrun.run_shell`` that records its call and returns the real
    run_shell JSON result shape (stdout/stderr/exit_code/duration_ms)."""

    async def fake_run_shell(command: str, **kwargs: Any) -> str:
        captured["calls"] = captured.get("calls", 0) + 1
        captured["command"] = command
        captured.update(kwargs)
        return json.dumps(
            {
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": exit_code,
                "duration_ms": 1.0,
            }
        )

    return fake_run_shell


def _install_arcrun_seam(
    monkeypatch: pytest.MonkeyPatch,
    captured: dict[str, Any],
    *,
    supports_vm: bool,
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
) -> None:
    """Patch the arcrun facade the runner is expected to call by qualified name.

    ``resolve_execution_backend`` delegates to the REAL pure router so the
    (tier, relax, platform_supports_vm) → backend mapping and the federal
    fail-closed raise are genuine. ``run_shell`` is a recording fake so no real
    container/VM is required. ``platform_supports_vm`` is forced so the VM-floor
    decision is deterministic.
    """
    from arcrun.builtins.execute import resolve_execution_backend as _real_resolve

    def resolve_spy(tier: str, *, relax: str | None, platform_supports_vm: bool) -> str:
        captured["resolve"] = {
            "tier": tier,
            "relax": relax,
            "platform_supports_vm": platform_supports_vm,
        }
        return _real_resolve(tier, relax=relax, platform_supports_vm=platform_supports_vm)

    monkeypatch.setattr(arcrun, "platform_supports_vm", lambda: supports_vm, raising=False)
    monkeypatch.setattr(arcrun, "resolve_execution_backend", resolve_spy, raising=False)
    monkeypatch.setattr(
        arcrun,
        "run_shell",
        _make_fake_run_shell(captured, stdout=stdout, stderr=stderr, exit_code=exit_code),
        raising=False,
    )


def _seed_skill(capabilities_root: Path, skill_name: str, script_relpath: str = "run.py") -> Path:
    """Create ``<capabilities_root>/skills/<skill_name>/<script_relpath>``."""
    folder = capabilities_root / "skills" / skill_name
    folder.mkdir(parents=True, exist_ok=True)
    script = folder / script_relpath
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("print('hello from skill')\n")
    return folder


def _sign_skill_script(skill_folder: Path, script_relpath: str = "run.py") -> frozenset[bytes]:
    """Sign the seeded script with a fresh keypair; return its public key as the
    trusted-key set to pin the runner to.

    Added for T-1072 (REQ-409): under the new integrity contract, enterprise and
    federal runs REQUIRE a signed script verified against a pinned key. These
    backend-selection / VM-floor tests keep their original intent — they just now
    hand the runner a properly signed fixture and its trusted key, so they exercise
    the happy path of the integrity gate instead of tripping its refusal.
    """
    identity = AgentIdentity.generate(org="blackarc", agent_type="executor")
    script = skill_folder / script_relpath
    artifact_signing.write_signature(
        script,
        script.read_bytes(),
        signer_did=identity.did,
        private_key=identity.signing_seed,
    )
    return frozenset({identity.public_key})


def _backend_selected_events(sink: _SpySink) -> list[Any]:
    return [e for e in sink.events if getattr(e, "action", None) == "code_exec.backend.selected"]


def _docker_available() -> bool:
    docker = shutil.which("docker")
    if docker is None:
        return False
    try:
        return (
            subprocess.run(
                [docker, "info"],
                capture_output=True,
                timeout=10,
            ).returncode
            == 0
        )
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# T-1062 (REQ-404, REQ-408) — personal tier
# --------------------------------------------------------------------------- #


class TestPersonalRun:
    async def test_run_returns_typed_result_not_string(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        _install_arcrun_seam(monkeypatch, captured, supports_vm=False, stdout="hello from skill\n")
        _seed_skill(tmp_path, "greeter")
        runner = SkillScriptRunner(capabilities_root=tmp_path, tier="personal")

        result = await runner.run("greeter", "run.py")

        # Typed data, never a string that could be spliced as instructions (REQ-408).
        assert isinstance(result, SkillScriptResult)
        assert not isinstance(result, str)
        assert result.stdout == "hello from skill\n"
        assert result.exit_code == 0

    async def test_run_mounts_skill_folder_as_workspace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        _install_arcrun_seam(monkeypatch, captured, supports_vm=False)
        skill_folder = _seed_skill(tmp_path, "greeter")
        runner = SkillScriptRunner(capabilities_root=tmp_path, tier="personal")

        await runner.run("greeter", "run.py")

        # arcrun.run_shell must be invoked with the skill's OWN folder as workspace.
        assert Path(captured["workspace"]).resolve() == skill_folder.resolve()
        assert captured["tier"] == "personal"

    async def test_run_invokes_python3_with_script_and_args(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        _install_arcrun_seam(monkeypatch, captured, supports_vm=False)
        _seed_skill(tmp_path, "greeter")
        runner = SkillScriptRunner(capabilities_root=tmp_path, tier="personal")

        await runner.run("greeter", "run.py", args=["--name", "arc"])

        command = captured["command"]
        assert "python3" in command
        assert "run.py" in command
        assert "--name" in command
        assert "arc" in command

    async def test_run_emits_backend_selected_audit_event(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        _install_arcrun_seam(monkeypatch, captured, supports_vm=False)
        _seed_skill(tmp_path, "greeter")
        sink = _SpySink()
        runner = SkillScriptRunner(
            capabilities_root=tmp_path,
            tier="personal",
            caller_did="did:arc:test:caller",
            audit_sink=sink,
        )

        await runner.run("greeter", "run.py")

        selected = _backend_selected_events(sink)
        assert selected, "runner must emit a code_exec.backend.selected audit event"
        event = selected[0]
        assert event.target == "docker"  # personal floor
        assert event.tier == "personal"
        assert event.actor_did == "did:arc:test:caller"

    async def test_result_carries_selected_backend_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        _install_arcrun_seam(monkeypatch, captured, supports_vm=False)
        _seed_skill(tmp_path, "greeter")
        runner = SkillScriptRunner(capabilities_root=tmp_path, tier="personal")

        result = await runner.run("greeter", "run.py")

        assert result.backend == "docker"


# --------------------------------------------------------------------------- #
# T-1064 (REQ-406, REQ-407) — enterprise folder jail, no network
# --------------------------------------------------------------------------- #


class TestEnterpriseJail:
    async def test_enterprise_selects_container_jailed_to_skill_folder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        _install_arcrun_seam(monkeypatch, captured, supports_vm=False)
        skill_folder = _seed_skill(tmp_path, "reporter")
        trusted = _sign_skill_script(skill_folder)
        runner = SkillScriptRunner(
            capabilities_root=tmp_path, tier="enterprise", trusted_public_keys=trusted
        )

        result = await runner.run("reporter", "run.py")

        # Container backend, jailed to the skill folder (DockerBackend enforces
        # --network=none and mounts only this workspace).
        assert result.backend == "docker"
        assert captured["tier"] == "enterprise"
        assert Path(captured["workspace"]).resolve() == skill_folder.resolve()

    async def test_enterprise_does_not_expose_parent_or_capabilities_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        _install_arcrun_seam(monkeypatch, captured, supports_vm=False)
        skill_folder = _seed_skill(tmp_path, "reporter")
        trusted = _sign_skill_script(skill_folder)
        runner = SkillScriptRunner(
            capabilities_root=tmp_path, tier="enterprise", trusted_public_keys=trusted
        )

        await runner.run("reporter", "run.py")

        workspace = Path(captured["workspace"]).resolve()
        # The writable root is the skill folder ONLY — never the capabilities
        # root, its skills dir, or any agent workspace above it.
        assert workspace == skill_folder.resolve()
        assert workspace != tmp_path.resolve()
        assert workspace != (tmp_path / "skills").resolve()
        assert workspace != tmp_path.parent.resolve()
        # No read-only subpath may point outside the skill folder either.
        for sub in captured.get("readonly_subpaths") or []:
            assert skill_folder.resolve() in Path(sub).resolve().parents or (
                Path(sub).resolve() == skill_folder.resolve()
            )

    @pytest.mark.parametrize(
        "bad_relpath",
        ["../secrets.py", "../../etc/shadow.py", "sub/../../escape.py"],
    )
    async def test_traversal_relpath_rejected_before_exec(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_relpath: str
    ) -> None:
        captured: dict[str, Any] = {}
        _install_arcrun_seam(monkeypatch, captured, supports_vm=False)
        _seed_skill(tmp_path, "reporter")
        runner = SkillScriptRunner(capabilities_root=tmp_path, tier="enterprise")

        with pytest.raises((ScriptPathError, SkillScriptError)):
            await runner.run("reporter", bad_relpath)

        # Rejected BEFORE any execution — arcrun.run_shell never called.
        assert captured.get("calls", 0) == 0

    async def test_absolute_script_path_rejected_before_exec(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        _install_arcrun_seam(monkeypatch, captured, supports_vm=False)
        _seed_skill(tmp_path, "reporter")
        runner = SkillScriptRunner(capabilities_root=tmp_path, tier="enterprise")

        with pytest.raises((ScriptPathError, SkillScriptError)):
            await runner.run("reporter", "/etc/passwd")

        assert captured.get("calls", 0) == 0

    @pytest.mark.slow
    @pytest.mark.requires_docker
    @pytest.mark.skipif(not _docker_available(), reason="no reachable Docker daemon")
    async def test_enterprise_real_sandbox_blocks_socket_and_out_of_folder(
        self, tmp_path: Path
    ) -> None:
        # Abuse case (real backend): a script that opens a network socket and
        # reads a file outside its folder must fail — network=none + folder jail.
        skill_folder = tmp_path / "skills" / "attacker"
        skill_folder.mkdir(parents=True)
        # The script tries to (a) open a network socket and (b) read the sibling
        # secret that lives OUTSIDE its mounted folder. Both must fail: the jail
        # mounts only the skill folder (so `../secret.txt` is not present) and
        # DockerBackend runs with --network=none. Reading a container-image file
        # like /etc/hostname is NOT a jail signal (every image ships one), so we
        # target the out-of-jail secret specifically.
        (skill_folder / "run.py").write_text(
            "import socket\n"
            "try:\n"
            "    socket.create_connection(('1.1.1.1', 53), timeout=3)\n"
            "    print('NET-OK')\n"
            "except Exception:\n"
            "    print('NET-BLOCKED')\n"
            "try:\n"
            "    print('OUT-READ:' + open('../secret.txt').read())\n"
            "except Exception:\n"
            "    print('OUT-BLOCKED')\n"
        )
        # A sibling secret OUTSIDE the mounted skill folder.
        (tmp_path / "skills" / "secret.txt").write_text("top secret\n")
        # Enterprise now requires a verified signature before exec (T-1072), so
        # sign the script and pin its key — this test exercises the JAIL, not the
        # integrity gate, so it must clear the gate to reach the real container.
        trusted = _sign_skill_script(skill_folder)
        runner = SkillScriptRunner(
            capabilities_root=tmp_path, tier="enterprise", trusted_public_keys=trusted
        )

        result = await runner.run("attacker", "run.py")

        assert "NET-BLOCKED" in result.stdout
        assert "OUT-BLOCKED" in result.stdout
        assert "top secret" not in result.stdout


# --------------------------------------------------------------------------- #
# T-1066 (REQ-405) — federal VM floor, fail closed
# --------------------------------------------------------------------------- #


class TestFederalFailClosed:
    async def test_federal_routes_to_vm_backend(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        _install_arcrun_seam(monkeypatch, captured, supports_vm=True)
        skill_folder = _seed_skill(tmp_path, "classified")
        trusted = _sign_skill_script(skill_folder)
        runner = SkillScriptRunner(
            capabilities_root=tmp_path, tier="federal", trusted_public_keys=trusted
        )

        result = await runner.run("classified", "run.py")

        assert result.backend == "vm"
        assert captured["tier"] == "federal"
        assert captured["resolve"]["tier"] == "federal"

    async def test_federal_fails_closed_when_vm_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        _install_arcrun_seam(monkeypatch, captured, supports_vm=False)
        skill_folder = _seed_skill(tmp_path, "classified")
        # Sign + pin so the integrity gate PASSES — this test's intent is the VM
        # fail-closed path, not signature refusal. With a valid signature the run
        # proceeds to backend selection, where federal-without-VM fails closed.
        trusted = _sign_skill_script(skill_folder)
        runner = SkillScriptRunner(
            capabilities_root=tmp_path, tier="federal", trusted_public_keys=trusted
        )

        # Fail closed — refuse, never downgrade to docker/local.
        with pytest.raises((IsolationUnavailableError, SkillScriptError)):
            await runner.run("classified", "run.py")

        # Never reached execution and never selected a weaker backend.
        assert captured.get("calls", 0) == 0
        selected_backend = captured.get("resolve", {}).get("platform_supports_vm", None)
        assert selected_backend is False


# --------------------------------------------------------------------------- #
# General — Python-only in this phase
# --------------------------------------------------------------------------- #


class TestScriptLanguageGate:
    @pytest.mark.parametrize("relpath", ["run.ts", "run.js", "run.sh", "run"])
    async def test_non_python_script_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relpath: str
    ) -> None:
        captured: dict[str, Any] = {}
        _install_arcrun_seam(monkeypatch, captured, supports_vm=False)
        folder = tmp_path / "skills" / "polyglot"
        folder.mkdir(parents=True)
        (folder / relpath).write_text("noop\n")
        runner = SkillScriptRunner(capabilities_root=tmp_path, tier="personal")

        with pytest.raises((UnsupportedScriptError, SkillScriptError)):
            await runner.run("polyglot", relpath)

        assert captured.get("calls", 0) == 0
