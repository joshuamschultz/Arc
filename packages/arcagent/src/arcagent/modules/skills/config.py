"""Thin skills-module config — the SkillAdapter seam's knobs (SPEC-044).

The skills module is *wiring only*: it owns no improvement logic (that lives in the
selected :class:`~arcagent.skilladapt.SkillAdapter`). These fields pick the adapter
and carry the improver's tuning block. ``adapter`` is the selector:

* ``"none"`` (default) — :class:`~arcagent.skilladapt.NullSkillAdapter`; off, zero files.
* ``"arcskill"`` — the ``arcskill.improver.ArcSkillImprover`` (the supported improver).
* a dotted ``module:Class`` path — a bring-your-own adapter (signed/allowlisted above personal).
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from arcagent.modules.base_config import ModuleConfig


class SkillsConfig(ModuleConfig):
    """Configuration for the thin skills (SkillAdapter) wiring module."""

    adapter: str = "none"
    tier: str = "personal"

    # Curator lifecycle-sweep cadence (Josh-LOCKED: all sweep settings config-adjustable).
    # The inactivity *window* (default 30 days) lives in the improver's LifecycleConfig;
    # this is how often the proactive engine fires the sweep. Default: daily.
    sweep_interval_seconds: float = Field(default=86_400.0, gt=0.0)

    # Operator-vetted BYO adapter class-paths. Above personal a dotted ``module:Class``
    # adapter is refused unless it appears here (ASI04 sign gate).
    adapter_allowlist: list[str] = Field(default_factory=list)

    # The ``[modules.skills.improver]`` block forwarded verbatim to the arcskill
    # ``ImproverConfig`` on construction (change_bound / lifecycle / thresholds).
    improver: dict[str, Any] = Field(default_factory=dict)


__all__ = ["SkillsConfig"]
