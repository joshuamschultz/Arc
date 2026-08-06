"""Configuration for the connector module.

Owned by the connector module — not part of core config.
Loaded from ``[modules.connectors.config]`` in arcagent.toml.

Two fields, both of them read by ``capabilities.py`` when it attaches the
configured connections. Each mirrors a path ``arc connector`` already lets an
operator override on the command line: a fleet whose bundles or operational
store live somewhere other than the default must be able to tell the agent the
same thing it told the CLI, or the agent looks in the wrong place for the
connections that command just installed. ``extra="forbid"`` (inherited from
``ModuleConfig``) turns a typo'd key into a loud validation error rather than a
silent no-op.
"""

from __future__ import annotations

from arcagent.core.module_config import ModuleConfig


class ConnectorsConfig(ModuleConfig):
    """Connector module configuration.

    Inherits ``extra="forbid"`` from ModuleConfig for typo detection.

    Attributes:
        extensions_root: Where this agent's bundles live. Empty means the
            ``extensions/`` directory beside ``arcagent.toml`` — the same
            default ``arc connector --extensions-root`` overrides.
        data_dir: The operational data plane holding approved tool-contract
            hashes. Empty defers to ``arcstore.resolve_data_dir``, so the agent
            reads the store the CLI wrote to.
    """

    extensions_root: str = ""
    data_dir: str = ""
