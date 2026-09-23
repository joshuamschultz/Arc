"""SPEC-081 T-1073 (REQ-404, REQ-406, REQ-409) — skill-NAME jail + fail-closed
integrity branches (RED wave for the code-review fixes).

A code review found a BLOCKING path-traversal in ``SkillScriptRunner.run``. Line
~111 computes the sandbox root straight from the caller-supplied ``skill_name``::

    skill_folder = (self._skills_root / skill_name).resolve()

with NO containment check on ``skill_folder`` itself. ``_resolve_script_inside``
then jails the *script relpath* to that folder — but if ``skill_name`` already
escaped (``"../evil"``, ``"../../etc"``, an absolute path, or an embedded
separator that resolves out), the jail is anchored OUTSIDE the skills root and
every downstream check passes. The runner then selects a backend and hands the
escaped directory to ``arcrun.run_shell`` as the sandbox workspace: arbitrary
out-of-jail directory mount and execution (ASI03 identity/privilege abuse via a
confused-deputy path, ASI05 unexpected code execution, LLM06 excessive agency).

This file pins the FIX contract for the skill-name jail, plus two fail-closed
integrity branches the review flagged as untested:

  1. ``skill_name`` MUST resolve INSIDE ``<capabilities_root>/skills`` or the run
     is refused with a typed ``SkillScriptError`` (``ScriptPathError`` or a new
     subclass) BEFORE any backend selection or ``run_shell`` call. A normal name
     still works.
  2. enterprise/federal with an EMPTY ``trusted_public_keys`` is an unpinned floor
     — a required verification with nothing to trust fails closed with
     ``ScriptIntegrityError`` before exec (regression lock; the branch already
     exists at skill_script_runner.py:175).
  3. when ``artifact_signing.verify_file`` itself raises, the runner maps it to
     ``ScriptIntegrityError`` (fail-closed) rather than leaking the underlying
     exception, and never executes (regression lock; branch at
     skill_script_runner.py:186).

RED-wave signal: the skill-name jail (finding #1) does NOT exist yet, so the
traversal cases reach ``run_shell`` and raise no error — the tests fail because
no ``SkillScriptError`` is raised and ``run_shell`` was called. Findings #2 and #3
are honest regression locks: the branches already raise, so those tests are
expected to pass on the current code and merely lock the behavior against future
regression. The test engineer does not force a false RED on already-correct code.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import arcrun
import pytest
from arctrust.identity import AgentIdentity

from arcagent.capabilities import artifact_signing
from arcagent.capabilities.skill_script_runner import (
    ScriptIntegrityError,
    ScriptPathError,
    SkillScriptError,
    SkillScriptResult,
    SkillScriptRunner,
)

# --------------------------------------------------------------------------- #
# Test doubles — recording audit sink + boundary-faithful arcrun seam (matches
# the style of the sibling integrity/runner suites so the mock story is identical).
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

    ``resolve_execution_backend`` delegates to the REAL pure router and records
    that it was CALLED. Any jail/integrity refusal MUST fire before it, so on
    refusal ``captured['resolve']`` stays absent — proving no backend was selected
    and no directory was ever handed to ``run_shell``.
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
    """Sign the seeded script with a fresh keypair; return its public key set."""
    identity = AgentIdentity.generate(org="blackarc", agent_type="executor")
    script = skill_folder / script_relpath
    artifact_signing.write_signature(
        script,
        script.read_bytes(),
        signer_did=identity.did,
        private_key=identity.signing_seed,
    )
    return frozenset({identity.public_key})


def _assert_refused_before_backend(captured: dict[str, Any], sink: _SpySink | None) -> None:
    """The refusal happened BEFORE any backend selection or execution."""
    assert captured.get("calls", 0) == 0, (
        "run_shell must never be called on a jail/integrity refusal"
    )
    assert "resolve" not in captured, "no backend may be selected on a jail/integrity refusal"
    if sink is not None:
        allow = [
            e
            for e in sink.events
            if getattr(e, "action", None) == "code_exec.backend.selected"
            and getattr(e, "outcome", None) == "allow"
        ]
        assert allow == [], "no backend-selected 'allow' event on a jail/integrity refusal"


# --------------------------------------------------------------------------- #
# Finding #1 — the BLOCKING traversal. skill_name must be jailed to the skills
# root BEFORE anything else. RED: currently escapes and reaches run_shell.
# --------------------------------------------------------------------------- #


class TestSkillNameJail:
    @pytest.mark.parametrize(
        "bad_skill_name",
        [
            "../evil",  # single parent escape
            "../../etc",  # double parent escape
            "sub/../../escape",  # embedded separator that resolves outside
            "/etc/evil",  # absolute path — os.path.join discards the skills root
        ],
    )
    async def test_escaping_skill_name_refused_before_exec(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_skill_name: str
    ) -> None:
        # Personal tier + no pinned keys deliberately isolates the JAIL from the
        # integrity gate: integrity is skipped for personal-without-keys, so if the
        # traversal is not rejected the run marches straight to run_shell — which is
        # exactly the review finding. A signed enterprise fixture could not tell a
        # jail failure apart from an integrity failure.
        captured: dict[str, Any] = {}
        _install_arcrun_seam(monkeypatch, captured, supports_vm=False)
        sink = _SpySink()
        runner = SkillScriptRunner(
            capabilities_root=tmp_path,
            tier="personal",
            caller_did="did:arc:test:caller",
            audit_sink=sink,
        )

        with pytest.raises((ScriptPathError, SkillScriptError)):
            await runner.run(bad_skill_name, "run.py")

        _assert_refused_before_backend(captured, sink)

    async def test_absolute_skill_name_via_tmp_refused_before_exec(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An absolute path that actually exists on disk but is OUTSIDE the skills
        # root: a real directory sitting beside the capabilities root. This proves
        # the jail rejects on containment, not on non-existence.
        captured: dict[str, Any] = {}
        _install_arcrun_seam(monkeypatch, captured, supports_vm=False)
        outside = tmp_path.parent / "outside_skill"
        (outside).mkdir(exist_ok=True)
        (outside / "run.py").write_text("print('escaped')\n")
        runner = SkillScriptRunner(capabilities_root=tmp_path, tier="personal")

        with pytest.raises((ScriptPathError, SkillScriptError)):
            await runner.run(str(outside), "run.py")

        _assert_refused_before_backend(captured, None)

    async def test_normal_skill_name_still_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Positive control: a legitimate skill name inside the skills root is
        # unaffected by the jail fix and still executes.
        captured: dict[str, Any] = {}
        _install_arcrun_seam(monkeypatch, captured, supports_vm=False)
        _seed_skill(tmp_path, "reporter")
        runner = SkillScriptRunner(capabilities_root=tmp_path, tier="personal")

        result = await runner.run("reporter", "run.py")

        assert isinstance(result, SkillScriptResult)
        assert captured.get("calls", 0) == 1


# --------------------------------------------------------------------------- #
# Re-review follow-up — the jail closed the gross escape but is still too loose:
# it only rejects names that resolve OUTSIDE the skills root. A ``skill_name`` that
# is not a single clean folder segment can still resolve INSIDE the root and be
# accepted, which is wrong. The intended contract: ``skill_name`` must be a single
# non-empty path segment that is not ``.``/``..`` and contains no separator,
# resolving to a DIRECT child of the skills root.
#
#   - "."               resolves to the skills root ITSELF -> would mount the WHOLE
#                        skills tree as the sandbox workspace. RED (currently allowed:
#                        the guard's ``candidate != skills_root`` short-circuits the
#                        containment check when the candidate IS the root).
#   - ".."              parent escape. Already refused by the containment check
#                        (regression lock — expected GREEN on current code).
#   - "a/b"             embedded separator (multi-segment) — resolves to a GRANDCHILD
#                        inside the root, not a direct child. RED (currently allowed).
#   - "skillA/../skillB" redirects to a SIBLING skill of the one named; resolves inside
#                        the root so the containment check misses it. RED (currently
#                        allowed) — a confused-deputy: caller asks for skillA, runs skillB.
#
# All refusals must fire as ``ScriptPathError`` BEFORE backend selection / run_shell.
# Personal tier + no pinned keys isolates the JAIL from the integrity gate exactly as
# TestSkillNameJail does, so an un-refused name marches straight to run_shell.
# --------------------------------------------------------------------------- #


class TestSkillNameSingleSegment:
    @pytest.mark.parametrize(
        "bad_skill_name",
        [
            ".",  # resolves to the skills root itself — mounts the whole tree
            "..",  # parent escape (already refused; regression lock)
            "a/b",  # embedded separator — multi-segment, grandchild not direct child
            "skillA/../skillB",  # redirects to a sibling skill than the one named
        ],
    )
    async def test_non_single_segment_skill_name_refused_before_exec(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_skill_name: str
    ) -> None:
        captured: dict[str, Any] = {}
        _install_arcrun_seam(monkeypatch, captured, supports_vm=False)
        sink = _SpySink()
        runner = SkillScriptRunner(
            capabilities_root=tmp_path,
            tier="personal",
            caller_did="did:arc:test:caller",
            audit_sink=sink,
        )

        with pytest.raises(ScriptPathError):
            await runner.run(bad_skill_name, "run.py")

        _assert_refused_before_backend(captured, sink)

    async def test_single_segment_skill_name_still_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Positive control: a clean single-segment name (direct child of the skills
        # root) is unaffected by the tightened jail and still executes.
        captured: dict[str, Any] = {}
        _install_arcrun_seam(monkeypatch, captured, supports_vm=False)
        _seed_skill(tmp_path, "greeter")
        runner = SkillScriptRunner(capabilities_root=tmp_path, tier="personal")

        result = await runner.run("greeter", "run.py")

        assert isinstance(result, SkillScriptResult)
        assert captured.get("calls", 0) == 1


# --------------------------------------------------------------------------- #
# Finding #2 — REGRESSION LOCK (expected GREEN on current code).
# enterprise/federal with EMPTY trusted_public_keys is an unpinned floor: a
# required verification with nothing to trust fails closed. Branch already exists
# at skill_script_runner.py:175 — this locks it against regression.
# --------------------------------------------------------------------------- #


class TestEmptyTrustedKeysFailClosed:
    @pytest.mark.parametrize("tier", ["enterprise", "federal"])
    async def test_signed_script_refused_when_no_key_pinned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tier: str
    ) -> None:
        # supports_vm=True so that, were the integrity gate ever bypassed, federal
        # backend selection would SUCCEED — proving the refusal is the unpinned-key
        # floor and not the VM fail-closed path.
        captured: dict[str, Any] = {}
        _install_arcrun_seam(monkeypatch, captured, supports_vm=True)
        skill_folder = _seed_skill(tmp_path, "reporter")
        _sign_script(skill_folder)  # genuinely signed, but NO key is pinned
        sink = _SpySink()
        runner = SkillScriptRunner(
            capabilities_root=tmp_path,
            tier=tier,
            trusted_public_keys=frozenset(),
            caller_did="did:arc:test:caller",
            audit_sink=sink,
        )

        with pytest.raises(ScriptIntegrityError):
            await runner.run("reporter", "run.py")

        _assert_refused_before_backend(captured, sink)


# --------------------------------------------------------------------------- #
# Finding #3 — REGRESSION LOCK (expected GREEN on current code).
# When verify_file itself raises, the runner maps to ScriptIntegrityError
# (fail-closed) and does NOT leak the underlying exception or execute. Branch
# already exists at skill_script_runner.py:186 — this locks it against regression.
# --------------------------------------------------------------------------- #


class TestVerificationRaisesFailClosed:
    async def test_verify_file_raising_maps_to_integrity_error_before_exec(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        _install_arcrun_seam(monkeypatch, captured, supports_vm=False)
        skill_folder = _seed_skill(tmp_path, "reporter")
        trusted = _sign_script(skill_folder)  # a key IS pinned, so we reach verify

        def _boom(*_args: Any, **_kwargs: Any) -> bool:
            raise RuntimeError("verify backend exploded")

        # Patch the symbol the runner actually calls (it does
        # ``from arcagent.capabilities import artifact_signing`` then
        # ``artifact_signing.verify_file(...)``).
        monkeypatch.setattr(
            "arcagent.capabilities.artifact_signing.verify_file", _boom, raising=True
        )
        sink = _SpySink()
        runner = SkillScriptRunner(
            capabilities_root=tmp_path,
            tier="enterprise",
            trusted_public_keys=trusted,
            caller_did="did:arc:test:caller",
            audit_sink=sink,
        )

        # Fail-closed: the runner's own typed error, NOT the raw RuntimeError.
        with pytest.raises(ScriptIntegrityError) as excinfo:
            await runner.run("reporter", "run.py")

        # The underlying cause is preserved for forensics but never leaked as the
        # raised type.
        assert isinstance(excinfo.value.__cause__, RuntimeError)
        _assert_refused_before_backend(captured, sink)
