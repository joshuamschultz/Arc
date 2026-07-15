"""Thin memory-module config — the Brain seam's generic knobs (SPEC-041 §4.6).

The memory module is *wiring only*: it owns no memory logic (that lives in the
selected :class:`~arcagent.brain.Brain`). These fields pick the brain and bound
recall + consolidation scheduling. ``brain`` is the SPEC-047 selector:

* ``"none"`` (default) — :class:`~arcagent.brain.NullBrain`; memory off, zero files.
* a backend name — that installed package's ``build_brain`` entrypoint.
* a dotted ``module:Class`` path — a bring-your-own Brain.

Backend-specific settings (an embedder, a distiller, decay/confidence knobs — whatever a
particular backend needs) live under the opaque :attr:`backend` dict, forwarded verbatim
to the selected backend's ``build_brain`` and validated there. This module stays ignorant
of every backend's field names, so it names no memory implementation.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from arcagent.core.module_config import ModuleConfig


class MemoryConfig(ModuleConfig):
    """Configuration for the thin memory (Brain) wiring module."""

    brain: str = "none"
    tier: str = "personal"

    # Operator-vetted BYO brain class-paths. Above the personal tier a dotted
    # ``module:Class`` brain is refused unless it appears here (ASI04 sign gate).
    brain_allowlist: list[str] = Field(default_factory=list)

    # Opaque, backend-defined settings forwarded verbatim to the selected backend's
    # ``build_brain(context)`` (as ``context["backend_config"]``). The backend validates
    # them; this thin module never reads a key, so it names no memory implementation.
    backend: dict[str, Any] = Field(default_factory=dict)

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
