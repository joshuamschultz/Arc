"""Config-driven Brain selection — the SPEC-041 pluggable-brain seam.

A thin :class:`ExtensionPoint` instance over the SPEC-047 generalized ``select_extension``
mechanism. Maps the ``[modules.memory] brain`` setting to a concrete :class:`Brain`:

* ``"none"``       → :class:`NullBrain` (default; memory off, zero files).
* ``"arcmemory"``  → ``arcmemory.ArcMemoryBrain`` (lazy import — arcagent has no
  static dependency on any memory package; missing install degrades to NullBrain
  with a warning rather than crashing the agent).
* ``"auto"``       → ``arcmemory`` if importable, else NullBrain (silent).
* dotted class path → a user-supplied Brain (BYO), instantiated ``cls(workspace, did)``;
  refused before import above personal unless operator-allowlisted (ASI04).

The choice dispatch, BYO allowlist gate, and dotted-path importer live once in
:func:`arcagent.extension.select.select_extension`; this module only supplies the
arcmemory builder and the BYO construction shape.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from arcagent.brain.protocol import Brain, NullBrain
from arcagent.extension import ExtensionPoint, select_extension

_logger = logging.getLogger("arcagent.brain.select")


def _build_arcmemory(module: Any, context: dict[str, Any]) -> Brain | None:
    """Build an arcllm-wired ``ArcMemoryBrain`` from the imported ``arcmemory`` module.

    The embedder + distiller seams are wired to arcllm (``ArcLLMEmbedder`` /
    ``ArcLLMDistiller``) so semantic vector recall, the analogical trigger channel, and
    consolidation insight-minting are live. ``embed_backend == "none"`` or an empty
    ``distill_provider`` leaves the respective seam unwired (recall degrades to BM25 +
    graph; consolidation is a no-op) — never a crash.
    """
    tier = context["tier"]
    safe_tier = tier if tier in ("personal", "enterprise", "federal") else "personal"
    config = module.MemoryConfig.for_tier(safe_tier)
    agent_did = context["agent_did"]
    embedder = _build_embedder(module, agent_did, context["embed_backend"], context["embed_model"])
    distiller = _build_distiller(
        module, context["distill_provider"], context["distill_model"], agent_did
    )
    brain: Brain = module.ArcMemoryBrain(
        context["workspace"],
        agent_did,
        config=config,
        embedder=embedder,
        distiller=distiller,
        audit_sink=context["audit_sink"],
        model=_build_loop_model(context["distill_provider"], context["distill_model"], agent_did),
        identity=context.get("identity"),
        policy_pipeline=context.get("policy_pipeline"),
    )
    return brain


def _build_loop_model(provider: str, model: str, agent_did: str) -> Any:
    """arcllm model handle for the agentic consolidation loop, or ``None`` when off.

    Same provider/model as the distiller, loaded WITH telemetry so the memory agent's
    turns ride the SPEC-038 budget/circuit-breaker (LLM10). ``None`` (no distill
    provider) → the agentic engine degrades to the pipeline distiller.
    """
    if not provider:
        return None
    import arcllm

    return arcllm.load_model(provider, model or None, telemetry={"agent_did": agent_did})


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


_BRAIN_POINT = ExtensionPoint(
    name="brain",
    null_factory=NullBrain,
    builtin_modules={"arcmemory": "arcmemory", "auto": "arcmemory"},
    builtin_builder=_build_arcmemory,
    byo_constructor=lambda cls, ctx: cls(ctx["workspace"], ctx["agent_did"]),
)


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
    identity: Any = None,
    policy_pipeline: Any = None,
) -> Brain:
    """Return the configured Brain (fail-safe: any degrade path yields NullBrain).

    ``identity`` (the agent's signing key) and ``policy_pipeline`` are threaded into the
    Brain so the agentic consolidation engine's memory-tool writes are signed and
    policy-authorized (fail-closed). ``None`` for either leaves the memory tools in the
    unconfigured-gate path (audit-only) — the single-dev default.
    """
    context: dict[str, Any] = {
        "workspace": workspace,
        "agent_did": agent_did,
        "tier": tier,
        "audit_sink": audit_sink,
        "embed_backend": embed_backend,
        "embed_model": embed_model,
        "distill_provider": distill_provider,
        "distill_model": distill_model,
        "identity": identity,
        "policy_pipeline": policy_pipeline,
    }
    brain: Brain = select_extension(
        _BRAIN_POINT,
        setting,
        tier=tier,
        allowlist=tuple(brain_allowlist),
        context=context,
        logger=_logger,
    )
    return brain


__all__ = ["select_brain"]
