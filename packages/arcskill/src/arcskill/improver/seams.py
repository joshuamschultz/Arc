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
from typing import Protocol, runtime_checkable


@runtime_checkable
class LLMInvoker(Protocol):
    """Structural contract for the LLM the judge + mutator drive (arcllm-backed)."""

    async def invoke(self, prompt: str) -> str: ...


@runtime_checkable
class Signer(Protocol):
    """Agent-DID sidecar signer (SPEC-033): sign ``content`` for ``path`` on write.

    The concrete impl writes the ``<path>.arcsig`` detached signature the hub
    re-verifies at reload. ``None`` (no signer) means personal-tier relaxable —
    no sidecar written.
    """

    def sign(self, path: Path, content: bytes) -> None: ...


__all__ = ["LLMInvoker", "Signer"]
