"""`arc agent create` emits three composable config files (post config-split).

The scaffold writes arcagent.toml + arcllm.toml + arcrun.toml into the agent
dir. Loading the agent must compose them: [llm]/[eval]/[budget] from arcllm.toml,
loop controls from arcrun.toml, everything else from arcagent.toml.
"""

from __future__ import annotations

import argparse
import tomllib
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_user_config(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path_factory.mktemp("empty-arc")))


def _create(tmp_path: Path, name: str, model: str = "anthropic/claude-sonnet-4-5-20250929") -> Path:
    from arccli.commands.agent.create import _create as create_cmd

    create_cmd(
        argparse.Namespace(
            name=name, parent_dir=str(tmp_path), model=model, no_register=True
        )
    )
    return tmp_path / name


class TestThreeFileScaffold:
    def test_emits_all_three_files(self, tmp_path: Path) -> None:
        agent_dir = _create(tmp_path, "trio")
        assert (agent_dir / "arcagent.toml").is_file()
        assert (agent_dir / "arcllm.toml").is_file()
        assert (agent_dir / "arcrun.toml").is_file()

    def test_llm_wire_only_in_arcllm_toml(self, tmp_path: Path) -> None:
        agent_dir = _create(tmp_path, "trio")
        arcagent = tomllib.loads((agent_dir / "arcagent.toml").read_text())
        arcllm = tomllib.loads((agent_dir / "arcllm.toml").read_text())
        # LLM-wire sections must NOT be in arcagent.toml.
        assert "llm" not in arcagent
        assert "eval" not in arcagent
        assert "budget" not in arcagent
        # They ARE in arcllm.toml.
        assert arcllm["llm"]["model"]
        assert "eval" in arcllm
        assert "budget" in arcllm

    def test_scaffold_composes_and_validates(self, tmp_path: Path) -> None:
        from arcagent.core.config import load_config

        agent_dir = _create(tmp_path, "trio")
        cfg = load_config(agent_dir / "arcagent.toml")
        # Real compose/boot: llm from arcllm.toml, loop cap from arcrun.toml,
        # a module setting from arcagent.toml.
        assert cfg.llm.model == "anthropic/claude-sonnet-4-5-20250929"
        assert cfg.llm.max_tokens == 8192
        assert cfg.eval.max_input_tokens == 100000
        assert cfg.arcrun.max_turns == 25
        assert cfg.modules["memory"].config["brain"] == "arcmemory"
        # [arcstore] is parsed ad-hoc (not an ArcAgentConfig field) — verify via loader.
        from arccli.commands.agent._store_lifecycle import load_arcstore_config

        assert load_arcstore_config(agent_dir).enabled is True

    def test_model_override_lands_in_arcllm(self, tmp_path: Path) -> None:
        from arcagent.core.config import load_config

        agent_dir = _create(tmp_path, "trio", model="openai/gpt-4o")
        cfg = load_config(agent_dir / "arcagent.toml")
        assert cfg.llm.model == "openai/gpt-4o"

    def test_env_override_still_wins_over_scaffold(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from arcagent.core.config import load_config

        agent_dir = _create(tmp_path, "trio")
        monkeypatch.setenv("ARCAGENT_ARCRUN__MAX_TURNS", "77")
        cfg = load_config(agent_dir / "arcagent.toml")
        assert cfg.arcrun.max_turns == 77
