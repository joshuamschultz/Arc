"""arcagent's memory boundary: the structural ``Brain`` Protocol + selection.

arcagent depends on no memory package. ``Brain`` is a structural Protocol and
``NullBrain`` the default no-op; :func:`select_brain` config-selects the impl
(``NullBrain`` / a named backend's ``build_brain`` / a BYO class path). See
:mod:`arcagent.brain.protocol`.
"""

from arcagent.brain.protocol import Brain, NullBrain
from arcagent.brain.select import select_brain

__all__ = ["Brain", "NullBrain", "select_brain"]
