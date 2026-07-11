"""Thin memory-module config — the Brain seam's knobs (SPEC-041 §4.6).

The memory module is *wiring only*: it owns no memory logic (that lives in the
selected :class:`~arcagent.brain.Brain`). These fields pick the brain and bound
recall + consolidation scheduling. ``brain`` is the SPEC-047 selector:

* ``"none"`` (default) — :class:`~arcagent.brain.NullBrain`; memory off, zero files.
* ``"arcmemory"`` / ``"auto"`` — the ``arcmemory`` plug-in if installed.
* a dotted ``module:Class`` path — a bring-your-own Brain.
"""

from __future__ import annotations

from pydantic import Field

from arcagent.modules.base_config import ModuleConfig


class MemoryConfig(ModuleConfig):
    """Configuration for the thin memory (Brain) wiring module."""

    brain: str = "none"
    tier: str = "personal"

    # Operator-vetted BYO brain class-paths. Above the personal tier a dotted
    # ``module:Class`` brain is refused unless it appears here (ASI04 sign gate).
    brain_allowlist: list[str] = Field(default_factory=list)

    # Embedder seam (arcmemory semantic + analogical-trigger channels). ``local``
    # wires arcllm's offline model; ``none`` degrades recall to BM25 + graph.
    embed_backend: str = "local"
    embed_model: str = ""  # empty -> arcllm default (all-MiniLM-L6-v2)

    # Distiller seam (arcmemory consolidation: fact extraction + insight minting).
    # Empty ``distill_provider`` leaves distillation off (consolidation is a no-op).
    distill_provider: str = ""
    distill_model: str = ""

    # Recall (agent:assemble_prompt @ priority 50)
    top_k: int = 5
    budget: int = 1024

    # Consolidation scheduling: fires on ANY of event-count / idle / interval (DC-5).
    consolidate_event_threshold: int = 20
    consolidate_idle_seconds: float = 900.0
    # Time-based cadence: consolidate at least this often while events are pending
    # (default hourly), so curated memory stays fresh even on a steady low volume.
    consolidate_interval_seconds: float = 3600.0


__all__ = ["MemoryConfig"]
