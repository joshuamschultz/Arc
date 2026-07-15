"""The Brain-provider entrypoint — arcmemory's side of arcagent's generic seam.

arcagent depends on no memory package. Its generic memory adapter resolves the
``[modules.memory] brain`` setting to a backend module *by name*, lazily imports it,
and calls its well-known ``build_brain(context)`` factory. This module is that factory
for arcmemory: it owns constructing an :class:`ArcMemoryBrain` — wiring the arcllm-backed
embedder / distiller / consolidation-loop seams — from the generic context dict.

The context dict is arcagent-owned and names nothing arcmemory-specific at the top level::

    {
        "workspace": Path,           # agent workspace root
        "agent_did": str,            # bound identity (memory requires identity)
        "tier": str,                 # personal | enterprise | federal
        "audit_sink": AuditSink,     # where mutations are audited
        "identity": AgentIdentity,   # signer for the sleep-pass agent's tool writes
        "policy_pipeline": ...,      # authorizer for the sleep-pass agent's tool writes
        "backend_config": {...},     # opaque, backend-defined (parsed below)
    }

``backend_config`` is arcmemory's own passthrough surface, forwarded verbatim from the
agent TOML's ``[modules.memory.config.backend]``. arcmemory validates it here so arcagent
never learns an arcmemory field name.
"""

from __future__ import annotations

from typing import Any

import arcllm

from arcmemory.arcllm_seam import ArcLLMDistiller, ArcLLMEmbedder
from arcmemory.brain import ArcMemoryBrain
from arcmemory.config import MemoryConfig, Tier


def build_brain(context: dict[str, Any]) -> ArcMemoryBrain:
    """Build an arcllm-wired :class:`ArcMemoryBrain` from arcagent's generic context.

    The embedder + distiller seams are wired to arcllm (:class:`ArcLLMEmbedder` /
    :class:`ArcLLMDistiller`) so semantic vector recall, the analogical trigger channel,
    and consolidation insight-minting are live. ``embed_backend == "none"`` or an empty
    ``distill_provider`` leaves the respective seam unwired (recall degrades to BM25 +
    graph; consolidation is a no-op) — never a crash.
    """
    backend = context.get("backend_config") or {}
    tier: Tier = _safe_tier(context.get("tier", "personal"))
    config = MemoryConfig.for_tier(tier)
    dynamics = backend.get("dynamics") or {}
    if dynamics:
        # Toml-supplied overrides applied OVER the tier defaults, re-validated by arcmemory.
        config = MemoryConfig(**{**config.model_dump(), **dynamics})

    agent_did = context["agent_did"]
    embed_backend = str(backend.get("embed_backend", "local"))
    embed_model = str(backend.get("embed_model", ""))
    distill_provider = str(backend.get("distill_provider", ""))
    distill_model = str(backend.get("distill_model", ""))

    return ArcMemoryBrain(
        context["workspace"],
        agent_did,
        config=config,
        embedder=build_embedder(agent_did, embed_backend, embed_model),
        distiller=build_distiller(distill_provider, distill_model, agent_did),
        audit_sink=context.get("audit_sink"),
        model=_build_loop_model(distill_provider, distill_model, agent_did),
        identity=context.get("identity"),
        policy_pipeline=context.get("policy_pipeline"),
    )


def _safe_tier(tier: object) -> Tier:
    """Coerce an untrusted tier string to a valid :class:`Tier`, defaulting to personal."""
    if tier in ("personal", "enterprise", "federal"):
        return tier  # type: ignore[return-value]  # reason: value ∈ the Tier Literal members; mypy can't narrow object to Literal
    return "personal"


def _build_loop_model(provider: str, model: str, agent_did: str) -> Any:
    """arcllm model handle for the agentic consolidation loop, or ``None`` when off.

    Same provider/model as the distiller, loaded WITH telemetry so the memory agent's
    turns ride the SPEC-038 budget/circuit-breaker (LLM10). ``None`` (no distill
    provider) → the agentic engine degrades to the pipeline distiller.
    """
    if not provider:
        return None
    return arcllm.load_model(provider, model or None, telemetry={"agent_did": agent_did})


def build_embedder(agent_did: str, backend: str, model: str) -> ArcLLMEmbedder | None:
    """arcllm-backed embedder, or ``None`` when the backend is explicitly off."""
    if backend == "none":
        return None
    telemetry = {"agent_did": agent_did}
    return ArcLLMEmbedder(model=model or None, backend=backend, telemetry=telemetry)


def build_distiller(provider: str, model: str, agent_did: str) -> ArcLLMDistiller | None:
    """arcllm-backed distiller (fresh provider per consolidation), or ``None`` when off.

    The per-run provider is loaded WITH telemetry so its ``invoke`` rides the SPEC-038
    budget/circuit-breaker (LLM10) — exactly as the embedder seam does; a runaway
    consolidation cannot make an unbounded distillation call.
    """
    if not provider:
        return None
    telemetry = {"agent_did": agent_did}

    def factory() -> Any:
        return arcllm.load_model(provider, model or None, telemetry=telemetry)

    return ArcLLMDistiller(factory, model=model or None)


__all__ = ["build_brain", "build_distiller", "build_embedder"]
