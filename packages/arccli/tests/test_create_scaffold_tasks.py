"""`arc agent create`'s scaffolded arcagent.toml must enable the tasks module.

SPEC-056 shipped the tasks module (`arcagent.modules.tasks`) with ten working
tools, but the agent-load path only registers a module's capabilities when
`[modules.<name>]` is declared+enabled in the agent's arcagent.toml
(agent_lifecycle.py: it iterates ``agent._config.modules`` and adds
``module:<name>`` to the capability scan roots only for enabled entries).

`_DEFAULT_CONFIG` — the template `arc agent create` writes — omitted
[modules.tasks], so every freshly-scaffolded agent shipped with Mission Control
silently off: the tools existed but never registered. This is the same
producers-unwired gap already fixed once for [modules.skills]
(test_init_skills_scaffold.py).

This test guards the scaffold so the tasks module can never regress back to
dead-on-arrival.
"""

from __future__ import annotations

import tomllib

from arccli.commands.agent._common import _DEFAULT_CONFIG


def test_default_config_declares_tasks_module_enabled() -> None:
    """The scaffold declares [modules.tasks] with the enabled/config envelope."""
    parsed = tomllib.loads(_DEFAULT_CONFIG.format(name="scaffold-agent"))
    tasks = parsed["modules"]["tasks"]
    assert tasks["enabled"] is True
    # nats_url mirrors messaging so assign_task can resolve @handles over the bus.
    assert tasks["config"]["nats_url"] == "nats://127.0.0.1:4222"


def test_default_config_tasks_block_round_trips_through_real_config_model() -> None:
    """The generated [modules.tasks] table must satisfy the real models.

    ModuleConfig uses extra="forbid", so a typo'd or mis-nested key (config
    fields as siblings of enabled/priority/config instead of under .config)
    raises here — a raw-text check would not catch it.
    """
    from arcagent.core.config import ModuleEntry
    from arcagent.modules.tasks.config import TasksConfig

    parsed = tomllib.loads(_DEFAULT_CONFIG.format(name="scaffold-agent"))

    # ModuleEntry.enabled is the sole load gate (agent_lifecycle iterates
    # config.modules and registers a module's capabilities only when its entry
    # is enabled) — the tasks module reads no separate config.enabled flag.
    tasks_entry = ModuleEntry.model_validate(parsed["modules"]["tasks"])
    assert tasks_entry.enabled is True

    tasks_config = TasksConfig.model_validate(tasks_entry.config)
    assert tasks_config.nats_url == "nats://127.0.0.1:4222"
