"""Tiered skill-script runner (SPEC-081 Phase 2, COMP-005).

Runs a promoted skill's own script inside arcrun's tier-selected isolation
backend, jailed to the skill's OWN folder as the only sandbox root, and returns
the script's output as TYPED result data — never a raw string that a caller
could splice back into a model's instructions (LLM05).

Concern purity: this layer never reimplements isolation or LLM-call logic. It
resolves the backend and runs the command through the ``arcrun`` root facade
(``resolve_execution_backend`` / ``platform_supports_vm`` / ``run_shell``) — no
deep import of ``arcrun.builtins``. Tier→backend mapping (federal→vm fail-closed,
enterprise/personal→container) lives in arcrun; the runner only selects, jails to
the skill folder, and emits the backend-selection audit event at this single
emission point.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import arcrun
from arctrust import AuditEvent, emit

from arcagent.capabilities import artifact_signing

# Sentinel actor DID for a runner invoked without a caller identity.
_RUNNER_ACTOR = "did:arc:system:skill-script-runner"

# Backend-name → declared isolation level, for the audit record's ``extra``.
_ISOLATION = {"vm": "vm", "docker": "container", "local": "none"}


class SkillScriptError(Exception):
    """Base for every skill-script-runner refusal."""


class ScriptPathError(SkillScriptError):
    """The requested script path escapes the skill folder (traversal/absolute)."""


class UnsupportedScriptError(SkillScriptError):
    """The script's language is not supported in this phase (Python-only)."""


class ScriptIntegrityError(SkillScriptError):
    """The resolved script's current bytes do not verify against a pinned key.

    Raised for a missing ``.arcsig`` sidecar, a post-sign content mutation
    (TOCTOU / supply-chain tamper), or a signature that matches no pinned trusted
    key. A required verification with no pinned key is the same unpinned-floor
    refusal — no key means nothing to trust, so it fails closed (ASI04, ASI05,
    LLM03).
    """


@dataclass(frozen=True)
class SkillScriptResult:
    """Typed result of a skill-script run — data, never instructions."""

    stdout: str
    stderr: str
    exit_code: int
    backend: str


@dataclass(frozen=True)
class SkillScriptRunner:
    """Run a promoted skill's script in a tier-isolated, folder-jailed sandbox."""

    capabilities_root: Path
    tier: str
    trusted_public_keys: frozenset[bytes] = frozenset()
    relax: str | None = None
    caller_did: str | None = None
    audit_sink: Any | None = None
    timeout: float = 30.0
    _skills_root: Path = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # Frozen dataclass: object.__setattr__ is the sanctioned way to set a
        # derived field once at construction.
        object.__setattr__(self, "_skills_root", Path(self.capabilities_root) / "skills")

    async def run(
        self,
        skill_name: str,
        script_relpath: str,
        *,
        args: list[str] | None = None,
    ) -> SkillScriptResult:
        """Resolve, jail, isolate, audit, and run one skill script.

        Order is security-first: the path jail and language gate reject before
        any backend is loaded or any command runs. Backend selection then routes
        through arcrun (federal-without-VM fails closed and propagates). Only
        after a backend is selected does the command execute.

        Raises:
            ScriptPathError: ``script_relpath`` escapes the skill folder.
            UnsupportedScriptError: the script is not a ``.py`` file.
            ScriptIntegrityError: verification is required for this tier and the
                resolved script's current bytes do not verify against a pinned
                key — refused before any backend selection or execution.
            arcrun.ExecutionIsolationError: tier isolation cannot be provided
                (e.g. federal with no VM support) — never downgraded.
        """
        skill_folder = (self._skills_root / skill_name).resolve()
        script_path = self._resolve_script_inside(skill_folder, script_relpath)

        if script_path.suffix != ".py":
            raise UnsupportedScriptError(
                f"unsupported script {script_relpath!r}; only .py is supported in this phase."
            )

        if self._integrity_required():
            self._verify_script_integrity(script_path)

        backend = self._select_backend()
        self._emit_backend_selected(backend, outcome="allow")

        command = self._build_command(script_relpath, args or [])
        raw = await arcrun.run_shell(
            command,
            tier=self.tier,
            workspace=skill_folder,
            relax=self.relax,
            caller_did=self.caller_did,
            audit_sink=self.audit_sink,
            timeout=self.timeout,
        )
        return self._parse_result(raw, backend)

    def _resolve_script_inside(self, skill_folder: Path, script_relpath: str) -> Path:
        """Resolve ``script_relpath`` and confirm it stays inside the skill folder.

        Rejects absolute paths and ``..`` traversal by resolving the candidate and
        checking containment — collapsing ``..`` before the comparison.
        """
        candidate = (skill_folder / script_relpath).resolve()
        if candidate != skill_folder and not candidate.is_relative_to(skill_folder):
            raise ScriptPathError(
                f"script path {script_relpath!r} escapes skill folder {skill_folder}."
            )
        return candidate

    def _integrity_required(self) -> bool:
        """Whether the resolved script must verify before it may run.

        Enterprise and federal ALWAYS require a verified signature. Personal
        requires it only when trusted keys are pinned — a documented dev-flexibility
        allowance lets an unsigned script run when no key is configured, but a
        pinned personal deployment still refuses a tampered one.
        """
        if self.tier in ("enterprise", "federal"):
            return True
        return bool(self.trusted_public_keys)

    def _verify_script_integrity(self, script_path: Path) -> None:
        """Re-verify the script's CURRENT bytes against its ``.arcsig`` sidecar.

        Mirrors the loader's pinned-key loop: accept only when a sidecar exists,
        the content hash matches (no post-sign tamper), and the signature verifies
        against at least one pinned trusted key. A required verification with no
        pinned key is an unpinned floor — nothing to trust — so it fails closed.
        Any verification error is likewise refused.

        Raises:
            ScriptIntegrityError: no pinned key, no matching key, missing sidecar,
                content tamper, or an error during verification.
        """
        if not self.trusted_public_keys:
            raise ScriptIntegrityError(
                f"script {script_path.name!r} requires a verified signature "
                "but no trusted key is pinned."
            )
        try:
            content = script_path.read_bytes()
            verified = any(
                artifact_signing.verify_file(script_path, content, trusted_public_key=key)
                for key in self.trusted_public_keys
            )
        except Exception as exc:  # reason: fail-closed — any verification error refuses
            raise ScriptIntegrityError(
                f"integrity verification failed for script {script_path.name!r}: {exc}"
            ) from exc
        if not verified:
            raise ScriptIntegrityError(
                f"script {script_path.name!r} failed signature verification "
                "(missing sidecar, post-sign tamper, or no pinned key matches)."
            )

    def _select_backend(self) -> str:
        """Resolve the tier-appropriate backend via arcrun; fail closed on refusal.

        A refusal (e.g. federal with no VM) is audited here — the single emission
        point — then re-raised unchanged so the caller never sees a downgrade.
        """
        try:
            return arcrun.resolve_execution_backend(
                self.tier,
                relax=self.relax,
                platform_supports_vm=arcrun.platform_supports_vm(),
            )
        except arcrun.ExecutionIsolationError:
            self._emit_backend_selected("<refused>", outcome="refuse")
            raise

    def _emit_backend_selected(self, backend: str, *, outcome: str) -> None:
        """Emit ``code_exec.backend.selected`` at this layer's single emission point."""
        if self.audit_sink is None:
            return
        event = AuditEvent(
            actor_did=self.caller_did or _RUNNER_ACTOR,
            action="code_exec.backend.selected",
            target=backend,
            outcome=outcome,
            tier=self.tier,
            extra={
                "isolation": _ISOLATION.get(backend, "none"),
                "relax": self.relax,
            },
        )
        emit(event, self.audit_sink)

    @staticmethod
    def _build_command(script_relpath: str, args: list[str]) -> str:
        """Build the ``python3 <script> <args...>`` command with shell-safe quoting."""
        parts = ["python3", shlex.quote(script_relpath), *(shlex.quote(a) for a in args)]
        return " ".join(parts)

    @staticmethod
    def _parse_result(raw: str, backend: str) -> SkillScriptResult:
        """Parse arcrun's JSON result shape into a typed ``SkillScriptResult``."""
        data = json.loads(raw)
        return SkillScriptResult(
            stdout=data["stdout"],
            stderr=data["stderr"],
            exit_code=data["exit_code"],
            backend=backend,
        )


__all__ = [
    "ScriptIntegrityError",
    "ScriptPathError",
    "SkillScriptError",
    "SkillScriptResult",
    "SkillScriptRunner",
    "UnsupportedScriptError",
]
