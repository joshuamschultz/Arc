"""SPEC-081 T-1072 (REQ-404, REQ-409) — skill-script runner signature gate (RED wave).

The Phase 2 runner (``test_skill_script_runner_spec081.py``) selects an isolation
backend and executes a promoted skill's ``.py`` with NO integrity check. That is a
TOCTOU / supply-chain hole (ASI04 agentic supply chain, ASI05 unexpected code
execution, LLM03 supply chain): the load-time trust gate re-verifies only
``SKILL.md``, so a script signed at promotion and mutated afterward — or never
signed at all — still runs. The execute-scripts capability this spec opens turns
that hole into arbitrary code execution.

This file pins the FIX contract: before the runner selects a backend or invokes
``arcrun.run_shell`` at tier ``enterprise`` or ``federal``, the resolved script
file MUST carry a valid ``.arcsig`` sidecar that verifies against a pinned trusted
key. Missing sidecar, post-sign mutation, or no-pinned-key-matches → a typed
``ScriptIntegrityError`` and NO backend selection, NO execution. Personal tier is
a documented decision: unsigned runs when no keys are configured (dev flexibility),
but a tampered script is still refused when keys ARE configured.

RED-wave signal: ``ScriptIntegrityError`` does not exist yet, so the import below
fails at collection. That is the correct "feature absent" failure — the same
convention the Phase 2 file documents for a not-yet-built interface.

Contract decision (test engineer): ``trusted_public_keys`` is typed
``frozenset[bytes]`` — raw Ed25519 verify keys — to match
``artifact_signing.verify_file(trusted_public_key: bytes)`` and the capability
loader's existing ``trusted_public_keys: tuple[bytes, ...]`` key material, rather
than the hex ``frozenset[str]`` floated in the task. Keys are the same bytes the
signer already produces; no hex round-trip is introduced.

Test → task/requirement map (all T-1072, REQ-404 execute-scripts + REQ-409 verify):
  test_enterprise_signed_script_runs                     — signed → backend + run.
  test_enterprise_tampered_script_refused_before_exec    — post-sign mutation → refuse.
  test_enterprise_unsigned_script_refused                — no sidecar → refuse.
  test_enterprise_unpinned_key_refused                   — signed by non-trusted key → refuse.
  test_federal_tampered_script_refused_before_vm         — tamper refused independent of VM.
  test_personal_unsigned_runs_when_no_keys_configured    — documented allowance.
  test_personal_signed_script_runs_when_keys_configured  — positive personal-with-keys.
  test_personal_tampered_script_refused_when_keys_configured — negative personal-with-keys.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import arcrun
import pytest
from arctrust.identity import AgentIdentity

from arcagent.capabilities import artifact_signing

# NOTE: ``ScriptIntegrityError`` is EXPECTED to be missing in the RED wave — the
# fix is not built yet. The ImportError at collection is the correct feature-absent
# signal; the names below define the interface the GREEN coder must implement.
from arcagent.capabilities.skill_script_runner import (
    ScriptIntegrityError,
    SkillScriptError,
    SkillScriptResult,
    SkillScriptRunner,
)

# --------------------------------------------------------------------------- #
# Test doubles — recording audit sink + boundary-faithful arcrun seam.
# --------------------------------------------------------------------------- #


class _SpySink:
    """arctrust AuditSink: any object with ``write(event) -> None``."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def write(self, event: Any) -> None:
        self.events.append(event)


def _make_fake_run_shell(captured: dict[str, Any]) -> Any:
    async def fake_run_shell(command: str, **kwargs: Any) -> str:
        captured["calls"] = captured.get("calls", 0) + 1
        captured["command"] = command
        captured.update(kwargs)
        return json.dumps({"stdout": "ran\n", "stderr": "", "exit_code": 0, "duration_ms": 1.0})

    return fake_run_shell


def _install_arcrun_seam(
    monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any], *, supports_vm: bool
) -> None:
    """Patch the arcrun facade the runner calls. Real backend router, fake shell.

    ``resolve_execution_backend`` delegates to the REAL pure router (so tier
    mapping and federal fail-closed are genuine) and records that it was CALLED —
    the integrity gate must run BEFORE it, so on refusal ``captured['resolve']``
    stays absent, proving no backend was selected.
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
    monkeypatch.setattr(arcrun, "run_shell", _make_fake_run_shell(captured), raising=False)


def _seed_skill(capabilities_root: Path, skill_name: str, script_relpath: str = "run.py") -> Path:
    folder = capabilities_root / "skills" / skill_name
    folder.mkdir(parents=True, exist_ok=True)
    script = folder / script_relpath
    script.write_text("print('hello from skill')\n")
    return folder


def _sign_script(skill_folder: Path, script_relpath: str = "run.py") -> frozenset[bytes]:
    """Sign the seeded script with a fresh keypair; return its public key as the
    trusted-key set to pin the runner to."""
    identity = AgentIdentity.generate(org="blackarc", agent_type="executor")
    script = skill_folder / script_relpath
    artifact_signing.write_signature(
        script,
        script.read_bytes(),
        signer_did=identity.did,
        private_key=identity.signing_seed,
    )
    return frozenset({identity.public_key})


def _mutate_one_byte(skill_folder: Path, script_relpath: str = "run.py") -> None:
    """Append one byte to the script AFTER signing — the content hash no longer
    matches the sidecar (post-promotion tamper)."""
    script = skill_folder / script_relpath
    script.write_bytes(script.read_bytes() + b"#")


def _assert_refused_before_backend(captured: dict[str, Any], sink: _SpySink) -> None:
    """The integrity gate refused BEFORE any backend selection or execution."""
    assert captured.get("calls", 0) == 0, "run_shell must never be called on integrity failure"
    assert "resolve" not in captured, "no backend may be selected on integrity failure"
    allow = [
        e
        for e in sink.events
        if getattr(e, "action", None) == "code_exec.backend.selected"
        and getattr(e, "outcome", None) == "allow"
    ]
    assert allow == [], "no backend-selected 'allow' event may be emitted on integrity failure"


# --------------------------------------------------------------------------- #
# Enterprise — signature verification is mandatory.
# --------------------------------------------------------------------------- #


class TestEnterpriseIntegrity:
    async def test_enterprise_signed_script_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        _install_arcrun_seam(monkeypatch, captured, supports_vm=False)
        skill_folder = _seed_skill(tmp_path, "reporter")
        trusted = _sign_script(skill_folder)
        runner = SkillScriptRunner(
            capabilities_root=tmp_path, tier="enterprise", trusted_public_keys=trusted
        )

        result = await runner.run("reporter", "run.py")

        assert isinstance(result, SkillScriptResult)
        assert result.backend == "docker"
        assert captured.get("calls", 0) == 1

    async def test_enterprise_tampered_script_refused_before_exec(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        _install_arcrun_seam(monkeypatch, captured, supports_vm=False)
        skill_folder = _seed_skill(tmp_path, "reporter")
        trusted = _sign_script(skill_folder)
        _mutate_one_byte(skill_folder)  # tamper AFTER signing
        sink = _SpySink()
        runner = SkillScriptRunner(
            capabilities_root=tmp_path,
            tier="enterprise",
            trusted_public_keys=trusted,
            caller_did="did:arc:test:caller",
            audit_sink=sink,
        )

        with pytest.raises((ScriptIntegrityError, SkillScriptError)):
            await runner.run("reporter", "run.py")

        _assert_refused_before_backend(captured, sink)

    async def test_enterprise_unsigned_script_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        _install_arcrun_seam(monkeypatch, captured, supports_vm=False)
        _seed_skill(tmp_path, "reporter")  # no sidecar written
        # A trusted key is configured, but the script has no signature at all.
        identity = AgentIdentity.generate(org="blackarc", agent_type="executor")
        sink = _SpySink()
        runner = SkillScriptRunner(
            capabilities_root=tmp_path,
            tier="enterprise",
            trusted_public_keys=frozenset({identity.public_key}),
            audit_sink=sink,
        )

        with pytest.raises((ScriptIntegrityError, SkillScriptError)):
            await runner.run("reporter", "run.py")

        _assert_refused_before_backend(captured, sink)

    async def test_enterprise_unpinned_key_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        _install_arcrun_seam(monkeypatch, captured, supports_vm=False)
        skill_folder = _seed_skill(tmp_path, "reporter")
        _sign_script(skill_folder)  # signed by key A (discarded)
        # The runner pins a DIFFERENT key B — an attacker's self-signed script
        # must not pass just because it carries a self-consistent sidecar.
        other = AgentIdentity.generate(org="blackarc", agent_type="executor")
        sink = _SpySink()
        runner = SkillScriptRunner(
            capabilities_root=tmp_path,
            tier="enterprise",
            trusted_public_keys=frozenset({other.public_key}),
            audit_sink=sink,
        )

        with pytest.raises((ScriptIntegrityError, SkillScriptError)):
            await runner.run("reporter", "run.py")

        _assert_refused_before_backend(captured, sink)


# --------------------------------------------------------------------------- #
# Federal — tamper refused independent of VM availability.
# --------------------------------------------------------------------------- #


class TestFederalIntegrity:
    async def test_federal_tampered_script_refused_before_vm(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        # VM IS available, so the ONLY reason to refuse is integrity — this proves
        # the signature gate is independent of the federal VM fail-closed path.
        _install_arcrun_seam(monkeypatch, captured, supports_vm=True)
        skill_folder = _seed_skill(tmp_path, "classified")
        trusted = _sign_script(skill_folder)
        _mutate_one_byte(skill_folder)
        sink = _SpySink()
        runner = SkillScriptRunner(
            capabilities_root=tmp_path,
            tier="federal",
            trusted_public_keys=trusted,
            audit_sink=sink,
        )

        with pytest.raises((ScriptIntegrityError, SkillScriptError)):
            await runner.run("classified", "run.py")

        _assert_refused_before_backend(captured, sink)


# --------------------------------------------------------------------------- #
# Personal — documented tier decision (allow unsigned only when no keys pinned).
# --------------------------------------------------------------------------- #


class TestPersonalIntegrity:
    async def test_personal_unsigned_runs_when_no_keys_configured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        _install_arcrun_seam(monkeypatch, captured, supports_vm=False)
        _seed_skill(tmp_path, "greeter")  # unsigned
        runner = SkillScriptRunner(capabilities_root=tmp_path, tier="personal")

        result = await runner.run("greeter", "run.py")

        # Documented dev-flexibility allowance: personal + no pinned keys runs.
        assert result.backend == "docker"
        assert captured.get("calls", 0) == 1

    async def test_personal_signed_script_runs_when_keys_configured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        _install_arcrun_seam(monkeypatch, captured, supports_vm=False)
        skill_folder = _seed_skill(tmp_path, "greeter")
        trusted = _sign_script(skill_folder)
        runner = SkillScriptRunner(
            capabilities_root=tmp_path, tier="personal", trusted_public_keys=trusted
        )

        result = await runner.run("greeter", "run.py")

        assert result.backend == "docker"
        assert captured.get("calls", 0) == 1

    async def test_personal_tampered_script_refused_when_keys_configured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        _install_arcrun_seam(monkeypatch, captured, supports_vm=False)
        skill_folder = _seed_skill(tmp_path, "greeter")
        trusted = _sign_script(skill_folder)
        _mutate_one_byte(skill_folder)
        sink = _SpySink()
        runner = SkillScriptRunner(
            capabilities_root=tmp_path,
            tier="personal",
            trusted_public_keys=trusted,
            audit_sink=sink,
        )

        # If keys ARE pinned, personal verifies too — tamper is refused.
        with pytest.raises((ScriptIntegrityError, SkillScriptError)):
            await runner.run("greeter", "run.py")

        _assert_refused_before_backend(captured, sink)
