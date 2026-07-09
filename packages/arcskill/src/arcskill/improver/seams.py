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

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from arcskill.improver.models import BundlePatch, BundleView, EvalCase, EvalOutcome

# Operator-approval seam (D-10). A thin injected callable — the improver decides *when*
# approval is required per the tier ladder; the provider returns True to proceed. arcagent
# binds this to the shared SPEC-035/043 HumanGate (operator-signed, fail-closed at federal);
# ``None`` (no provider wired) means the improver fails closed when approval is required.
# ``(action, skill_name, detail) -> approved``.
ApprovalProvider = Callable[[str, str, str], Awaitable[bool]]


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
class Signer(Protocol):
    """Agent-DID sidecar signer (SPEC-033): sign ``content`` for ``path`` on write.

    The concrete impl writes the ``<path>.arcsig`` detached signature the hub
    re-verifies at reload. ``None`` (no signer) means personal-tier relaxable —
    no sidecar written.
    """

    def sign(self, path: Path, content: bytes) -> None: ...


__all__ = ["ApprovalProvider", "EvalRunner", "LLMInvoker", "Mutator", "Signer"]
