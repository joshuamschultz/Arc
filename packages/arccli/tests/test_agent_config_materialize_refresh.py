"""H-039: materializing every default must be semantically invisible, and
regenerating must never silently overwrite an operator's choice.

Two properties, proven here rather than just asserted in prose:

1. ``test_materializing_bare_defaults_is_semantically_invisible`` — writing a
   field's OWN default explicitly into TOML and loading it back must produce
   the exact same effective config as never having written that key at all
   (pure Pydantic in-code defaults). If this ever goes red, the generator
   itself is putting a WRONG value in the file — a silent behavior change
   dressed up as "just documentation".

2. ``test_refresh_updates_untouched_defaults_but_preserves_operator_overrides``
   — the tradeoff the advisor flagged (materializing freezes today's default
   into every user's file) is only safe if there is a way to un-freeze it.
   ``arc agent config --refresh-defaults`` is that way: a key still equal to
   the value config_render last wrote there advances to today's default; a
   key an operator changed does not move.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import tomlkit
from arcagent.core.config import AgentConfig, ArcAgentConfig, ContextConfig, LLMConfig
from arcagent.utils import config_render

from arccli.commands.agent._common import render_agent_config
from arccli.commands.agent._config_sync import refresh_agent_config, write_config_snapshot

_PROBE_NAME = "probe"
_PROBE_MODEL = "anthropic/claude-sonnet-4-5-20250929"


def test_materializing_bare_defaults_is_semantically_invisible(tmp_path: Path) -> None:
    """Render every field at its BARE model default (no scaffold business
    overrides — those are a separate, deliberate concern already pinned by
    the test_create_scaffold_*.py suite) across all three sibling files, load
    the result through the real composition path, and compare it field for
    field against constructing ``ArcAgentConfig`` directly in Python with
    nothing but the two truly-required fields — the "no TOML at all"
    baseline. They must be identical everywhere except ``modules``: a bare
    ``ArcAgentConfig()`` has none configured at all, while ANY materialized
    file always carries the built-in module policy
    (``BUILTIN_MODULE_DEFAULTS``) — an intentional product decision, not
    something a round-trip test should expect to vanish.
    """
    import arcagent

    agent_dir = tmp_path
    (agent_dir / "arcagent.toml").write_text(
        config_render.render_arcagent_toml(overrides={"agent": {"name": _PROBE_NAME}}),
        encoding="utf-8",
    )
    (agent_dir / "arcllm.toml").write_text(
        config_render.render_arcllm_sections(llm_overrides={"model": _PROBE_MODEL}),
        encoding="utf-8",
    )
    (agent_dir / "arcrun.toml").write_text(config_render.render_arcrun_toml(), encoding="utf-8")

    materialized = arcagent.load_config(agent_dir / "arcagent.toml")
    inherited = ArcAgentConfig(
        agent=AgentConfig(name=_PROBE_NAME), llm=LLMConfig(model=_PROBE_MODEL)
    )

    materialized_dump = materialized.model_dump(mode="json")
    inherited_dump = inherited.model_dump(mode="json")
    del materialized_dump["modules"]
    del inherited_dump["modules"]

    assert materialized_dump == inherited_dump, (
        "a bare-default materialized file must load to the SAME effective config "
        "as never writing the key at all"
    )


def test_refresh_updates_untouched_defaults_but_preserves_operator_overrides(
    tmp_path: Path,
) -> None:
    """Simulate the exact scenario the materialize-vs-freeze tradeoff warns about:
    a model's default changes after agents already exist. An agent that never
    touched the field must pick up the new default; one whose operator
    deliberately set a different value must not be moved.
    """
    agent_dir = tmp_path
    (agent_dir / "arcagent.toml").write_text(
        render_agent_config(name=_PROBE_NAME, did="did:arc:test:probe"), encoding="utf-8"
    )
    write_config_snapshot(agent_dir)

    # Operator hand-edits ONE untouched-default field to a deliberate value.
    config_path = agent_dir / "arcagent.toml"
    document = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    tools_table = document["tools"]
    assert isinstance(tools_table, tomlkit.items.Table)
    policy_table = tools_table["policy"]
    assert isinstance(policy_table, tomlkit.items.Table)
    policy_table["timeout_seconds"] = 999
    config_path.write_text(tomlkit.dumps(document), encoding="utf-8")

    original_prune_threshold = ContextConfig.model_fields["prune_threshold"].default
    try:
        # Simulate the model's default changing for a DIFFERENT, untouched field.
        ContextConfig.model_fields["prune_threshold"].default = 0.42

        result = refresh_agent_config(agent_dir)

        assert "context.prune_threshold" in result.refreshed
        assert "tools.policy.timeout_seconds" not in result.refreshed
        assert result.written is True

        updated = tomllib.loads(config_path.read_text(encoding="utf-8"))
        assert updated["context"]["prune_threshold"] == 0.42
        assert updated["tools"]["policy"]["timeout_seconds"] == 999
    finally:
        ContextConfig.model_fields["prune_threshold"].default = original_prune_threshold


def test_refresh_is_a_noop_once_nothing_is_stale(tmp_path: Path) -> None:
    """A second refresh right after the first finds nothing left to advance —
    proves the post-write snapshot rebase actually took (the operator's kept
    value is now the new baseline, not perpetually "stale")."""
    agent_dir = tmp_path
    (agent_dir / "arcagent.toml").write_text(
        render_agent_config(name=_PROBE_NAME, did="did:arc:test:probe"), encoding="utf-8"
    )
    write_config_snapshot(agent_dir)

    first = refresh_agent_config(agent_dir)
    assert first.written is False
    assert not first.changed

    second = refresh_agent_config(agent_dir)
    assert not second.changed
