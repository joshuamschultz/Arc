"""Configuration for the connector module.

Owned by the connector module — not part of core config.
Loaded from ``[modules.connectors.config]`` in arcagent.toml.

Three fields, all of them read by ``capabilities.py`` when it attaches the
connections this agent has been granted. Each mirrors a path ``arc connector``
already lets an operator override on the command line: a fleet whose bundles,
grants, or operational store live somewhere other than the default must be able
to tell the agent the same thing it told the CLI, or the agent looks in the wrong
place for the connection that command just made. ``extra="forbid"`` (inherited
from ``ModuleConfig``) turns a typo'd key into a loud validation error rather
than a silent no-op.
"""

from __future__ import annotations

from arcagent.core.module_config import ModuleConfig


class ConnectorsConfig(ModuleConfig):
    """Connector module configuration.

    Inherits ``extra="forbid"`` from ModuleConfig for typo detection.

    Attributes:
        arc_dir: The deployment config root holding ``connections.toml`` (the
            grants) and ``connections.env`` (the credentials) — the same thing
            ``arc connector --arc-dir`` means. Empty defers to ``arc_home()``, so
            an agent and the CLI read one deployment without either being
            configured.
        extensions_root: The ONE directory this agent's bundles live in — the
            same thing ``arc connector --extensions-root`` means. Empty means the
            deployment's ordered search path instead (``<agent>/extensions``,
            ``$ARC_EXTENSIONS_ROOT``, ``<arc_home>/extensions``), so a fleet
            ships its bundles once rather than once per agent (D-584).
        data_dir: The operational data plane holding approved tool-contract
            hashes. Empty defers to ``arcstore.resolve_data_dir``, so the agent
            reads the store the CLI wrote to.
    """

    arc_dir: str = ""
    extensions_root: str = ""
    data_dir: str = ""
