"""Injected Protocol seams — arcskill.improver declares, arcagent injects (REQ-004, D-3).

``arcskill.improver`` is provider-free: LLM completion, skill writes, sandbox
evaluation, and audit all enter through these structural Protocols. arcagent's
``skilladapt`` wiring supplies concrete implementations (arcllm-backed LLM, the
operator-anchored :class:`SkillRevisionWriter`, the ``hub.dry_run`` sandbox runner,
the operator-key WORM sink).

There is one signing authority for skill content: the operator, through the
anchored revision chain. The improver proposes changes and commits them through
``SkillRevisionWriter``; it holds no signer and never writes a skill file in place.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
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
class Merger(Protocol):
    """Proposes a consolidated skill from two overlapping skills (Curator consolidate).

    The default production impl is :class:`~arcskill.improver.mutate.LLMSkillMerger`
    (arcllm-backed via :class:`LLMInvoker`); deterministic fakes satisfy it in tests.
    Returns ``None`` when no safe merge is proposed — consolidation then no-ops for
    that pair. The returned :class:`BundlePatch` carries the merged ``SKILL.md`` body
    under ``files["SKILL.md"]`` — the same shape a code patch uses, so it flows through
    the identical sign + gate + apply machinery (never a bespoke merge-apply path).
    """

    async def propose(
        self, *, a: BundleView, b: BundleView, insight: str
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


class SkillWriterUnavailableError(RuntimeError):
    """No operator-anchored revision writer is wired; the improver must not write."""


class SkillRevisionRefusedError(ValueError):
    """The revision writer refused a commit (unsafe path, stale head, bad signature)."""


@runtime_checkable
class SkillRevisionWriter(Protocol):
    """The ONE write path for an improver change: an operator-anchored skill revision.

    The improver never writes or signs a skill file itself. It hands the changed
    files (skill-root-relative posix paths -> new bytes) to this seam; every file it
    does not name carries forward. arcagent backs it with the anchored revision
    chain signed by the OPERATOR signer, so the agent's DID key never signs a
    capability artifact, and history, versions and rollback show the change.
    ``reason`` labels the commit for audit. Returns the new revision digest.
    Raises ``ValueError`` (nothing activated) for an unknown skill, an unsafe path,
    or any authority/verification failure. ``None`` (no writer wired) means the
    improver fails closed: status ``unavailable``, nothing written.
    """

    def commit(self, skill_name: str, files: Mapping[str, bytes], *, reason: str) -> str: ...


__all__ = [
    "ApprovalProvider",
    "EvalRunner",
    "LLMInvoker",
    "Merger",
    "Mutator",
    "SkillRevisionRefusedError",
    "SkillRevisionWriter",
    "SkillWriterUnavailableError",
]
