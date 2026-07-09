"""arcagent's skill-self-improvement boundary: the ``SkillAdapter`` seam + selection.

arcagent ships improver-less by default. ``SkillAdapter`` is a structural Protocol and
``NullSkillAdapter`` the default no-op; :func:`select_skill_adapter` config-selects the
impl (``NullSkillAdapter`` / ``arcskill`` / a signed BYO class path). Mirrors
:mod:`arcagent.brain`. See :mod:`arcagent.skilladapt.protocol`.
"""

from arcagent.skilladapt.protocol import NullSkillAdapter, SkillAdapter
from arcagent.skilladapt.select import select_skill_adapter

__all__ = ["NullSkillAdapter", "SkillAdapter", "select_skill_adapter"]
