"""Skills module — thin config-enabled wiring for the SkillAdapter seam (SPEC-044).

Holds no improvement logic: it selects a :class:`~arcagent.skilladapt.SkillAdapter`
(``none`` / ``arcskill`` / signed BYO) and forwards primitive per-turn signals to it.
Improver-less by default.
"""

from arcagent.modules.skills.config import SkillsConfig

__all__ = ["SkillsConfig"]
