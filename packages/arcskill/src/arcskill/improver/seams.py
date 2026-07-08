"""Injected Protocol seams — arcskill.improver declares, arcagent injects (REQ-004, D-3).

``arcskill.improver`` is provider-free: LLM completion, artifact signing, sandbox
evaluation, and audit all enter through these structural Protocols. arcagent's
``skilladapt`` wiring supplies concrete implementations (arcllm-backed LLM, the
agent-DID sidecar signer, the ``hub.dry_run`` sandbox runner, the operator-key
WORM sink).

Phase 1 ships the two seams the relocated engine needs — ``LLMInvoker`` (drives
the judge + prose mutator) and ``Signer`` (agent-DID sidecar on write). The
richer ``Mutator``/``Judge``/``EvalRunner``/``AuditSink`` Protocols over
``BundleView`` land as the acceptance path is rewired (SPEC-044 Phases 3-4).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from arcskill.improver.models import BundlePatch, BundleView, EvalCase, EvalOutcome


@runtime_checkable
class LLMInvoker(Protocol):
    """Structural contract for the LLM the judge + mutator drive (arcllm-backed)."""

    async def invoke(self, prompt: str) -> str: ...


@runtime_checkable
class Mutator(Protocol):
    """Proposes a code-repair patch from failing traces (REQ-010/011, D-1c GEPA).

    The default production impl is :class:`~arcskill.improver.mutate.LLMCodeMutator`
    (arcllm-backed via :class:`LLMInvoker`); deterministic fakes satisfy it in tests.
    Returns ``None`` when no safe patch is proposed — the code path then no-ops.
    """

    async def propose(
        self, *, kind: str, current: BundleView, failures: str, insight: str
    ) -> BundlePatch | None: ...


@runtime_checkable
class EvalRunner(Protocol):
    """Runs a skill's golden-task suite in isolation; the security boundary (REQ-023).

    The default production impl is a thin adapter over ``arcskill.hub.dry_run``
    (Firecracker federal / Docker fallback, ``SandboxRequired`` fail-closed — DC-5).
    Returns one :class:`EvalOutcome` per :class:`EvalCase`. Deterministic fakes
    satisfy it in unit tests — the injected boundary, not a rigged fixture.
    """

    async def run(self, view: BundleView, cases: list[EvalCase]) -> list[EvalOutcome]: ...


@runtime_checkable
class Approver(Protocol):
    """Operator-approval seam for consequential transitions (D-10, SPEC-043 ladder).

    arcskill declares it; arcagent injects a real approver that reuses the SPEC-043
    tier HITL mechanism. ``request`` returns ``True`` to proceed, ``False`` to block.
    The improver decides *when* approval is required (per-tier ladder) — federal gates
    every mutation + retire/revive, enterprise gates code mutations, personal auto —
    and fails closed (blocks) when approval is required but no approver is wired.
    """

    async def request(self, *, action: str, skill_name: str, detail: str) -> bool: ...


@runtime_checkable
class Signer(Protocol):
    """Agent-DID sidecar signer (SPEC-033): sign ``content`` for ``path`` on write.

    The concrete impl writes the ``<path>.arcsig`` detached signature the hub
    re-verifies at reload. ``None`` (no signer) means personal-tier relaxable —
    no sidecar written.
    """

    def sign(self, path: Path, content: bytes) -> None: ...


__all__ = ["Approver", "EvalRunner", "LLMInvoker", "Mutator", "Signer"]
