"""Configuration for the policy module.

Owned by the policy module — not part of core config.
Loaded from ``[modules.policy.config]`` in arcagent.toml.
Validated internally by the module on construction.
"""

from __future__ import annotations

from pydantic import BaseModel


class PolicyConfig(BaseModel):
    """Policy module configuration.

    Controls the ACE-based self-learning policy engine:
    evaluation frequency, bullet limits, and text constraints.
    """

    eval_interval_turns: int = 20
    max_bullets: int = 200
    max_bullet_text_length: int = 500
