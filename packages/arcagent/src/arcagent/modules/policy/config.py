"""Configuration for the policy module.

Owned by the policy module — not part of core config.
Loaded from ``[modules.policy.config]`` in arcagent.toml.
Validated internally by the module on construction.
"""

from __future__ import annotations

from arcagent.modules.base_config import ModuleConfig


class PolicyConfig(ModuleConfig):
    """Policy module configuration.

    Controls the ACE-based self-learning policy engine:
    evaluation frequency, bullet limits, and text constraints.
    Inherits ``extra="forbid"`` from ModuleConfig for typo detection.
    """

    # Interactive turn cadence for the two policy eval jobs. A "turn" is one
    # eval-eligible ``agent:post_respond`` cycle (see ``periodic_policy_eval``).
    # policy eval: the periodic Reflector over the live transcript.
    eval_interval_turns: int = 50
    # daily-notes eval: the grounded reflection that rides on ``memory.consolidated``.
    # Throttled to a turn cadence so it stops firing on every consolidation pass.
    daily_notes_every_turns: int = 20
    max_bullets: int = 200
    max_bullet_text_length: int = 500

    # Idle-flush backstop: once this many wall-clock seconds have elapsed since the
    # last policy eval AND the turn counter has advanced, evaluate on the next turn
    # even below ``eval_interval_turns``. Guarantees a slow session still learns
    # instead of stalling just under the cadence boundary.
    flush_idle_seconds: int = 900

    # Grounded-reflection write approval (SPEC-041 Phase 9). Federal stages the
    # curated bullets to ``policy.pending`` for human review; personal/enterprise
    # auto-apply to ``policy.md``.
    tier: str = "personal"
