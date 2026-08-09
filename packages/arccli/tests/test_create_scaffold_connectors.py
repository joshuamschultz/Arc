"""`arc agent create`'s scaffolded arcagent.toml must enable the connectors module.

SPEC-062 shipped the connector module (`arcagent.modules.connectors`) and the
eight-verb `arc connector` surface, but the agent-load path only registers a
module's capabilities when `[modules.<name>]` is declared+enabled in the agent's
arcagent.toml (agent_lifecycle.py iterates ``agent._config.modules`` and adds
``module:<name>`` to the capability scan roots only for enabled entries).

Without this block, `arc connector add` writes a connection nothing ever serves:
the credential lands, the config block lands, and the agent starts with none of
the verbs. That is the producers-unwired shape already fixed once for
[modules.skills] and once for [modules.tasks] — this test is what stops the
third instance.

The scaffold is exercised through the real render used by `arc agent create`,
and the block is validated by the module's own config model rather than by
string matching, so a mis-nested key fails here.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from arccli.commands.agent._common import render_agent_config


def test_default_config_declares_connectors_module_enabled() -> None:
    """The scaffold declares [modules.connectors] with the enabled/config envelope."""
    parsed = tomllib.loads(render_agent_config(name="scaffold-agent"))
    connectors = parsed["modules"]["connectors"]
    assert connectors["enabled"] is True
    # Both paths default to empty: bundles come from the search path and the
    # contract store from arcstore, which is what an unconfigured agent needs.
    assert connectors["config"]["arc_dir"] == ""
    assert connectors["config"]["extensions_root"] == ""
    assert connectors["config"]["data_dir"] == ""


def test_default_config_connectors_block_round_trips_through_real_config_model() -> None:
    """The generated [modules.connectors] table must satisfy the real models.

    ModuleConfig uses extra="forbid", so a typo'd or mis-nested key (config
    fields as siblings of enabled/priority/config instead of under .config)
    raises here — a raw-text check would not catch it.
    """
    from arcagent.core.config import ModuleEntry
    from arcagent.modules.connectors.config import ConnectorsConfig

    parsed = tomllib.loads(render_agent_config(name="scaffold-agent"))

    entry = ModuleEntry.model_validate(parsed["modules"]["connectors"])
    assert entry.enabled is True

    config = ConnectorsConfig.model_validate(entry.config)
    assert config.arc_dir == ""
    assert config.extensions_root == ""
    assert config.data_dir == ""


def test_a_scaffolded_agent_loads_with_the_connectors_module_active(tmp_path: Path) -> None:
    """Through the real config loader: the written file parses and the module is on.

    ``arc agent create`` writes this text to disk and the agent reads it back
    with ``load_config``; asserting on the rendered string alone would not prove
    the file an operator ends up with actually enables anything.
    """
    from arcagent.core.config import load_config

    config_path = tmp_path / "arcagent.toml"
    config_path.write_text(render_agent_config(name="scaffold-agent"), encoding="utf-8")

    config = load_config(config_path)

    assert config.modules["connectors"].enabled is True
