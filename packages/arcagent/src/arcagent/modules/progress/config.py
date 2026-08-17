"""Configuration for the progress-narration module.

Loaded from ``[modules.progress.config]`` in arcagent.toml. The module's ON/OFF
switch is the ``[modules.progress]`` entry itself — a module that is not enabled
never loads, so there is deliberately no second ``enabled`` flag here to leave
switched off by accident.

Every value below exists to answer one question: how much is too much? A silent
agent is bad, but a chatty one is worse, so the defaults are deliberately quiet.
"""

from __future__ import annotations

from pydantic import Field

from arcagent.core.module_config import ModuleConfig


class ProgressConfig(ModuleConfig):
    """Pacing limits for run narration."""

    # Child agents start in bursts. Waiting this long before announcing them
    # turns "started an agent" x4 into one "Started 4 agents on this step."
    coalesce_seconds: float = Field(default=1.5, ge=0.0, le=30.0)

    # Floor on the gap between two ordinary lines. A stage name the model emits
    # in a tight loop is dropped rather than queued: a late progress line is
    # worse than no progress line. Batch results and endings ignore this.
    min_gap_seconds: float = Field(default=10.0, ge=0.0)

    # Hard ceiling per run (LLM10). Whatever the script does, the person gets at
    # most this many messages about it.
    max_lines_per_run: int = Field(default=12, ge=1)

    # Stage names are written by the model, so they are cut to this length
    # before being quoted back to a person (LLM01 / ASI09).
    max_step_chars: int = Field(default=90, ge=20, le=400)


__all__ = ["ProgressConfig"]
