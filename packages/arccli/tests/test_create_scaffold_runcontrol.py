"""`arc agent create`'s scaffolded arcagent.toml must enable the runcontrol module.

The run-control watcher (`arcagent.modules.runcontrol`) is what APPLIES operator
cancel requests (`arc stop` / the arcui cancel route write them regardless) by
cooperatively cancelling the matching live tracked run. Like every module, it
only registers when `[modules.runcontrol]` is declared+enabled in the agent's
arcagent.toml — the same producers-unwired gap already guarded for tasks and
skills. This test guards the scaffold so a freshly-created agent is stoppable
without SSH out of the box.
"""

from __future__ import annotations

import tomllib

from arccli.commands.agent._common import _DEFAULT_CONFIG


def test_default_config_declares_runcontrol_module_enabled() -> None:
    parsed = tomllib.loads(_DEFAULT_CONFIG.format(name="scaffold-agent"))
    rc = parsed["modules"]["runcontrol"]
    assert rc["enabled"] is True


def test_default_config_runcontrol_block_round_trips_through_real_config_model() -> None:
    """The generated [modules.runcontrol] table must satisfy the real models.

    RuncontrolConfig uses extra="forbid", so a stray/mis-nested key raises here.
    """
    from arcagent.core.config import ModuleEntry
    from arcagent.modules.runcontrol.config import RuncontrolConfig

    parsed = tomllib.loads(_DEFAULT_CONFIG.format(name="scaffold-agent"))
    entry = ModuleEntry.model_validate(parsed["modules"]["runcontrol"])
    assert entry.enabled is True

    rc_config = RuncontrolConfig.model_validate(entry.config)
    assert rc_config.stale_ttl_seconds == 300
