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

    eval_interval_turns: int = 20
    max_bullets: int = 200
    max_bullet_text_length: int = 500

    # Grounded-reflection write approval (SPEC-041 Phase 9). Federal stages the
    # curated bullets to ``policy.pending`` for human review; personal/enterprise
    # auto-apply to ``policy.md``.
    tier: str = "personal"
