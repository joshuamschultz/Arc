"""Config-driven Brain selection — the SPEC-047 pluggable-brain seam.

Maps the ``[modules.memory] brain`` setting to a concrete :class:`Brain`:

* ``"none"``       → :class:`NullBrain` (default; memory off, zero files).
* ``"arcmemory"``  → ``arcmemory.ArcMemoryBrain`` (lazy import — arcagent has no
  static dependency on any memory package; missing install degrades to NullBrain
  with a warning rather than crashing the agent).
* ``"auto"``       → ``arcmemory`` if importable, else NullBrain.
* dotted class path → a user-supplied Brain (BYO), instantiated ``cls(workspace, did)``.

arcagent never imports a memory type at module load; the only ``import arcmemory``
is lazy, inside :func:`select_brain`, and guarded.
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Any

from arcagent.brain.protocol import Brain, NullBrain

_logger = logging.getLogger("arcagent.brain.select")


def select_brain(
    setting: str,
    *,
    workspace: Path,
    agent_did: str,
    tier: str = "personal",
    audit_sink: Any = None,
    embed_backend: str = "local",
    embed_model: str = "",
    distill_provider: str = "",
    distill_model: str = "",
    brain_allowlist: tuple[str, ...] = (),
) -> Brain:
    """Return the configured Brain (fail-safe: any error degrades to NullBrain).

    When arcmemory is selected and importable, its embedder + distiller seams are
    wired to arcllm (:class:`arcmemory.ArcLLMEmbedder` /
    :class:`arcmemory.ArcLLMDistiller`) so semantic vector recall, the analogical
    trigger channel, and consolidation insight-minting are live. ``embed_backend
    == "none"`` or an empty ``distill_provider`` leaves the respective seam unwired
    (recall degrades to BM25 + graph; consolidation is a no-op) — never a crash.

    A dotted BYO class-path is arbitrary code executed at startup (ASI04). Above the
    personal tier it is refused unless it appears in ``brain_allowlist`` (the operator's
    signed/vetted registry) — fail-closed, never imported (see :func:`_load_custom`).
    """
    choice = (setting or "none").strip()
    if choice in ("none", "", "null"):
        return NullBrain()
    if choice in ("arcmemory", "auto"):
        brain = _try_arcmemory(
            workspace,
            agent_did,
            tier,
            audit_sink,
            embed_backend=embed_backend,
            embed_model=embed_model,
            distill_provider=distill_provider,
            distill_model=distill_model,
        )
        if brain is not None:
            return brain
        if choice == "arcmemory":
            _logger.warning(
                "memory brain='arcmemory' but arcmemory is not installed; "
                "running memory-less (NullBrain)"
            )
        return NullBrain()
    return _load_custom(choice, workspace, agent_did, tier=tier, allowlist=brain_allowlist)


def _try_arcmemory(
    workspace: Path,
    agent_did: str,
    tier: str,
    audit_sink: Any,
    *,
    embed_backend: str,
    embed_model: str,
    distill_provider: str,
    distill_model: str,
) -> Brain | None:
    """Build an arcllm-wired ``ArcMemoryBrain`` if arcmemory is importable, else ``None``."""
    try:
        arcmemory = importlib.import_module("arcmemory")
    except ImportError:
        return None
    safe_tier = tier if tier in ("personal", "enterprise", "federal") else "personal"
    config = arcmemory.MemoryConfig.for_tier(safe_tier)
    embedder = _build_embedder(arcmemory, agent_did, embed_backend, embed_model)
    distiller = _build_distiller(arcmemory, distill_provider, distill_model, agent_did)
    brain: Brain = arcmemory.ArcMemoryBrain(
        workspace,
        agent_did,
        config=config,
        embedder=embedder,
        distiller=distiller,
        audit_sink=audit_sink,
    )
    return brain


def _build_embedder(arcmemory: Any, agent_did: str, backend: str, model: str) -> Any:
    """arcllm-backed embedder, or ``None`` when the backend is explicitly off."""
    if backend == "none":
        return None
    telemetry = {"agent_did": agent_did}
    return arcmemory.ArcLLMEmbedder(model=model or None, backend=backend, telemetry=telemetry)


def _build_distiller(arcmemory: Any, provider: str, model: str, agent_did: str) -> Any:
    """arcllm-backed distiller (fresh provider per consolidation), or ``None`` when off.

    The per-run provider is loaded WITH telemetry so its ``invoke`` rides the SPEC-038
    budget/circuit-breaker (LLM10) — exactly as the embedder seam does; a runaway
    consolidation cannot make an unbounded distillation call.
    """
    if not provider:
        return None
    import arcllm

    telemetry = {"agent_did": agent_did}

    def factory() -> Any:
        return arcllm.load_model(provider, model or None, telemetry=telemetry)

    return arcmemory.ArcLLMDistiller(factory, model=model or None)


def _load_custom(
    class_path: str, workspace: Path, agent_did: str, *, tier: str, allowlist: tuple[str, ...]
) -> Brain:
    """Import + instantiate a BYO Brain from a dotted ``module:Class`` / ``module.Class``.

    Above the personal tier a BYO class-path must be operator-allowlisted (the signed
    registry posture of SPEC-033) — otherwise it is REFUSED before any import, because
    importing an unverified dotted path is arbitrary code execution at startup (ASI04).
    """
    if tier != "personal" and class_path not in allowlist:
        raise ValueError(
            f"BYO brain class-path {class_path!r} is not on the operator allowlist; "
            f"refusing to import an unverified class-path at tier {tier!r} (fail-closed)"
        )
    module_name, _, attr = class_path.replace(":", ".").rpartition(".")
    if not module_name:
        raise ValueError(f"invalid brain class path: {class_path!r}")
    cls = getattr(importlib.import_module(module_name), attr)
    brain: Brain = cls(workspace, agent_did)
    return brain


__all__ = ["select_brain"]
