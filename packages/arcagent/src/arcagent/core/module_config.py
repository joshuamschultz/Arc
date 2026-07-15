"""Base configuration for all ArcAgent modules.

Module-framework infrastructure: provides ``ModuleConfig`` with
``extra="forbid"`` so misspelled module-config keys raise a validation error
instead of being silently ignored. Every module's ``config.py`` defines its
``<Name>Config`` on this base.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModuleConfig(BaseModel):
    """Base config for ArcAgent modules.

    Uses ``extra="forbid"`` so misspelled config keys raise
    a validation error instead of being silently ignored.
    """

    model_config = ConfigDict(extra="forbid")
