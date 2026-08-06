"""Configuration for the connector module.

Owned by the connector module — not part of core config.
Loaded from ``[modules.connectors.config]`` in arcagent.toml.

Deliberately empty for now: this module ships only the discovery + per-agent
runtime scaffold (SPEC-062 T-901/T-902). Fields land here as later SPEC-062
tasks wire manifest loading, attachments, and credentials through it — adding
them now, before anything reads them, would be exactly the premature field
CLAUDE.md's YAGNI rule forbids. ``extra="forbid"`` (inherited from
``ModuleConfig``) still turns a typo'd key into a loud validation error
rather than a silent no-op.
"""

from __future__ import annotations

from arcagent.core.module_config import ModuleConfig


class ConnectorsConfig(ModuleConfig):
    """Connector module configuration.

    Inherits ``extra="forbid"`` from ModuleConfig for typo detection.
    """
