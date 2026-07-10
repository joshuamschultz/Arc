"""`arc init`'s generated arcagent.toml must enable the skills module.

Root pyproject.toml declares arcskill as the default skills adapter
(SkillsConfig.adapter defaults to "none" otherwise), but `_arcagent_base_config`
omitted [modules.skills] entirely — every freshly-scaffolded personal-tier
agent shipped with skills silently off, matching the memory/skills adapter
gap already fixed once for arcgateway-telegram's token_env.

The correct shape is the generic module envelope (arcagent.core.config
ArcAgentConfig.modules: dict[str, ModuleEntry], where ModuleEntry has
enabled/priority/config) — module-specific fields (adapter, tier) live
UNDER [modules.skills.config], not flat under [modules.skills]. This
mirrors the existing [modules.memory] / [modules.memory.config] pattern in
the same function.
"""

from __future__ import annotations

from arccli.commands.init import _arcagent_base_config, _generate_arcagent_toml


def test_base_config_declares_skills_module_nested() -> None:
    """The skills module entry uses the enabled/config envelope, not a flat shape."""
    config = _arcagent_base_config("personal")
    skills = config["modules"]["skills"]
    assert skills["enabled"] is True
    assert skills["config"]["adapter"] == "arcskill"
    assert skills["config"]["tier"] == "personal"


def test_base_config_skills_tier_matches_selected_tier() -> None:
    config = _arcagent_base_config("enterprise")
    assert config["modules"]["skills"]["config"]["tier"] == "enterprise"


def test_generated_toml_skills_block_round_trips_through_real_config_model() -> None:
    """The generated TOML must actually be understood by ArcAgentConfig/SkillsConfig.

    _arcagent_base_config produces a partial config (no agent/identity — those
    come from the per-instance arcagent.toml it merges under), so this
    validates the [modules] table through the real ModuleEntry/SkillsConfig
    models rather than the full ArcAgentConfig. ModuleConfig uses
    extra="forbid": a regression to a flat [modules.skills] adapter="arcskill"
    (adapter/tier as siblings of enabled/priority/config, instead of nested
    under .config) would raise here — a raw-TOML text check would not catch it.
    """
    import tomllib

    from arcagent.core.config import ModuleEntry
    from arcagent.modules.skills.config import SkillsConfig

    toml_text, _ = _generate_arcagent_toml("personal")
    parsed = tomllib.loads(toml_text)

    skills_entry = ModuleEntry.model_validate(parsed["modules"]["skills"])
    assert skills_entry.enabled is True

    skills_config = SkillsConfig.model_validate(skills_entry.config)
    assert skills_config.adapter == "arcskill"
    assert skills_config.tier == "personal"
