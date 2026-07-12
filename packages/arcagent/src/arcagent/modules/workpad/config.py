"""Config for the workpad (self-managing ``context.md``) module.

The workpad module is a background maintainer: every ``every_n_runs`` real
(non-automated) runs it hands the current ``context.md`` plus the recent session
activity to the eval model and rewrites the file as a curated cockpit of open
loops. It is the SOLE writer of ``context.md`` — compaction no longer flushes to
it. See :mod:`arcagent.modules.workpad.capabilities`.
"""

from __future__ import annotations

from pydantic import Field

from arcagent.modules.base_config import ModuleConfig


class WorkpadConfig(ModuleConfig):
    """Configuration for the self-managing ``context.md`` maintainer."""

    # Cadence: rewrite context.md every N non-automated runs. ``ge=1`` guards the
    # ``run_count % every_n_runs`` trigger against a modulo-by-zero / never-fire 0.
    every_n_runs: int = Field(default=20, ge=1)

    # Bound on the recent-activity transcript fed to the maintainer (LLM10 /
    # unbounded consumption). Oldest lines are dropped once the accumulated
    # transcript would exceed this many characters.
    max_transcript_chars: int = Field(default=24000, ge=1000)

    # Hard cap on the rewritten context.md (LLM10 + ASI-06). A cockpit that grows
    # without bound stops being a cockpit; truncation is the backstop.
    max_context_chars: int = Field(default=8000, ge=1000)


__all__ = ["WorkpadConfig"]
