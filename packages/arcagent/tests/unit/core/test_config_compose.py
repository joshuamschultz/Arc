"""Three-file config composition — arcagent.toml + arcllm.toml + arcrun.toml.

The loader composes an agent's effective config from three sibling files in the
SAME directory:
  * ``arcagent.toml`` — agent/identity/tools/telemetry/security/modules/…
  * ``arcllm.toml``   — ``[llm]`` / ``[eval]`` / ``[budget]`` (LLM-wire)
  * ``arcrun.toml``   — the agentic-loop controls (``[arcrun]`` root)

Each file-family deep-merges independently (packaged defaults < user-wide
``${ARC_CONFIG_DIR}/<file>.toml`` < per-agent ``<dir>/<file>.toml``); the three
merged results are then composed into one :class:`ArcAgentConfig`. A missing
sibling falls through to packaged/Pydantic defaults.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from arcagent.core.config import DEFAULT_MODEL, load_config


def _write(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body))


@pytest.fixture(autouse=True)
def isolated_user_config(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    """Point the user-wide config root at an empty dir so the real ~/.arc can't leak.

    Layering tests that need a user-wide layer override ARC_CONFIG_DIR themselves.
    """
    empty = tmp_path_factory.mktemp("empty-arc")
    monkeypatch.setenv("ARC_CONFIG_DIR", str(empty))
    return empty


@pytest.fixture()
def agent_dir(tmp_path: Path) -> Path:
    """A per-agent directory with all three sibling config files."""
    _write(
        tmp_path / "arcagent.toml",
        """\
        [agent]
        name = "composed"

        [modules.memory]
        enabled = true

        [modules.memory.config]
        brain = "arcmemory"
        """,
    )
    _write(
        tmp_path / "arcllm.toml",
        """\
        [llm]
        model = "openai/gpt-4o"
        max_tokens = 2048

        [eval]
        model = "openai/gpt-4o-mini"
        max_tokens = 512

        [budget]
        max_cost_usd = 1.5
        """,
    )
    _write(
        tmp_path / "arcrun.toml",
        """\
        max_turns = 7
        tool_timeout = 42.0
        allowed_strategies = ["react"]

        [sandbox]
        allowed_tools = ["calculate"]
        """,
    )
    return tmp_path


class TestThreeFileCompose:
    def test_llm_comes_from_arcllm_toml(self, agent_dir: Path) -> None:
        cfg = load_config(agent_dir / "arcagent.toml")
        assert cfg.llm.model == "openai/gpt-4o"
        assert cfg.llm.max_tokens == 2048

    def test_eval_and_budget_come_from_arcllm_toml(self, agent_dir: Path) -> None:
        cfg = load_config(agent_dir / "arcagent.toml")
        assert cfg.eval.model == "openai/gpt-4o-mini"
        assert cfg.eval.max_tokens == 512
        assert cfg.budget.max_cost_usd == 1.5

    def test_loop_controls_come_from_arcrun_toml(self, agent_dir: Path) -> None:
        cfg = load_config(agent_dir / "arcagent.toml")
        assert cfg.arcrun.max_turns == 7
        assert cfg.arcrun.tool_timeout == 42.0
        assert cfg.arcrun.allowed_strategies == ["react"]
        assert cfg.arcrun.sandbox.allowed_tools == ["calculate"]

    def test_module_setting_comes_from_arcagent_toml(self, agent_dir: Path) -> None:
        cfg = load_config(agent_dir / "arcagent.toml")
        assert cfg.modules["memory"].enabled is True
        assert cfg.modules["memory"].config["brain"] == "arcmemory"


class TestMissingSiblingsFallThrough:
    def test_only_arcagent_toml_boots_with_defaults(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "arcagent.toml",
            """\
            [agent]
            name = "minimal"
            """,
        )
        cfg = load_config(tmp_path / "arcagent.toml")
        # arcllm.toml absent → packaged default model; arcrun.toml absent → defaults.
        assert cfg.llm.model == DEFAULT_MODEL
        assert cfg.arcrun.max_turns == 40
        assert cfg.eval.max_tokens == 1024  # EvalConfig default

    def test_llm_in_arcagent_toml_is_ignored(self, tmp_path: Path) -> None:
        """No back-compat: [llm] only ever loads from arcllm.toml."""
        _write(
            tmp_path / "arcagent.toml",
            """\
            [agent]
            name = "ignore-llm"

            [llm]
            model = "bogus/should-be-ignored"
            """,
        )
        cfg = load_config(tmp_path / "arcagent.toml")
        assert cfg.llm.model == DEFAULT_MODEL


class TestPerFileLayering:
    def test_per_agent_arcllm_overrides_user_wide(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_dir = tmp_path / "userarc"
        user_dir.mkdir()
        monkeypatch.setenv("ARC_CONFIG_DIR", str(user_dir))
        _write(
            user_dir / "arcllm.toml",
            """\
            [llm]
            model = "user/wide-model"
            temperature = 0.1
            """,
        )
        agent = tmp_path / "agent"
        agent.mkdir()
        _write(agent / "arcagent.toml", "[agent]\nname = \"layered\"\n")
        _write(agent / "arcllm.toml", "[llm]\nmodel = \"peragent/model\"\n")

        cfg = load_config(agent / "arcagent.toml")
        # Per-agent model wins; user-wide temperature still merges through.
        assert cfg.llm.model == "peragent/model"
        assert cfg.llm.temperature == 0.1

    def test_user_wide_arcrun_merges_when_no_per_agent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_dir = tmp_path / "userarc"
        user_dir.mkdir()
        monkeypatch.setenv("ARC_CONFIG_DIR", str(user_dir))
        _write(user_dir / "arcrun.toml", "max_turns = 3\n")
        agent = tmp_path / "agent"
        agent.mkdir()
        _write(agent / "arcagent.toml", "[agent]\nname = \"layered\"\n")

        cfg = load_config(agent / "arcagent.toml")
        assert cfg.arcrun.max_turns == 3


class TestEnvOverridesStillWin:
    def test_env_overrides_arcllm_model(
        self, agent_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ARCAGENT_LLM__MODEL", "env/override-model")
        cfg = load_config(agent_dir / "arcagent.toml")
        assert cfg.llm.model == "env/override-model"

    def test_env_overrides_arcrun_max_turns(
        self, agent_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ARCAGENT_ARCRUN__MAX_TURNS", "99")
        cfg = load_config(agent_dir / "arcagent.toml")
        assert cfg.arcrun.max_turns == 99
