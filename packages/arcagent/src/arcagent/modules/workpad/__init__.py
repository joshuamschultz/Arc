"""Workpad module — wires the self-managing ``context.md`` maintainer.

Sole writer of ``context.md``: a background eval-model call rewrites it as a
curated cockpit of open loops every ``every_n_runs`` runs. See
:mod:`arcagent.modules.workpad.capabilities`.
"""

from arcagent.modules.workpad.config import WorkpadConfig

__all__ = ["WorkpadConfig"]
