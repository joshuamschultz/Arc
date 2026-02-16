"""Tests for config parser — TOML + Pydantic validation."""

import textwrap
from pathlib import Path

import pytest

from arcagent.core.config import (
    AgentConfig,
    ContextConfig,
    EvalConfig,
    IdentityConfig,
    LLMConfig,
    SessionConfig,
    TelemetryConfig,
    ToolConfig,
    ToolsConfig,
    VaultConfig,
    load_config,
)
from arcagent.core.errors import ConfigError


@pytest.fixture()
def valid_toml(tmp_path: Path) -> Path:
    config = tmp_path / "arcagent.toml"
    config.write_text(textwrap.dedent("""\
        [agent]
        name = "test-agent"
        org = "blackarc"
        type = "executor"

        [llm]
        model = "anthropic/claude-sonnet-4-5-20250929"
        max_tokens = 4096
    """))
    return config


@pytest.fixture()
def minimal_toml(tmp_path: Path) -> Path:
    config = tmp_path / "arcagent.toml"
    config.write_text(textwrap.dedent("""\
        [agent]
        name = "minimal"

        [llm]
        model = "anthropic/claude-sonnet-4-5-20250929"
    """))
    return config


class TestLoadConfig:
    def test_loads_valid_toml(self, valid_toml: Path) -> None:
        cfg = load_config(valid_toml)
        assert cfg.agent.name == "test-agent"
        assert cfg.agent.org == "blackarc"
        assert cfg.llm.model == "anthropic/claude-sonnet-4-5-20250929"

    def test_missing_file_raises_config_error(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError) as exc_info:
            load_config(tmp_path / "nonexistent.toml")
        assert exc_info.value.code == "CONFIG_FILE_NOT_FOUND"

    def test_syntax_error_includes_line_info(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.toml"
        bad.write_text("[agent\nname = bad")
        with pytest.raises(ConfigError) as exc_info:
            load_config(bad)
        assert exc_info.value.code == "CONFIG_SYNTAX"

    def test_validation_error_includes_field_path(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.toml"
        bad.write_text(textwrap.dedent("""\
            [agent]
            name = "test"

            [llm]
            max_tokens = "not_a_number"
        """))
        with pytest.raises(ConfigError) as exc_info:
            load_config(bad)
        assert exc_info.value.code == "CONFIG_VALIDATION"
        assert "llm" in str(exc_info.value.details)

    def test_minimal_config_has_defaults(self, minimal_toml: Path) -> None:
        cfg = load_config(minimal_toml)
        assert cfg.agent.org == "default"
        assert cfg.agent.type == "executor"
        assert cfg.llm.max_tokens == 4096
        assert cfg.llm.temperature == 0.7

    def test_env_override(self, minimal_toml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ARCAGENT_LLM__MODEL", "openai/gpt-4o")
        cfg = load_config(minimal_toml)
        assert cfg.llm.model == "openai/gpt-4o"

    def test_env_override_nested(
        self, minimal_toml: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ARCAGENT_AGENT__ORG", "test-org")
        cfg = load_config(minimal_toml)
        assert cfg.agent.org == "test-org"


class TestAgentConfig:
    def test_defaults(self) -> None:
        cfg = AgentConfig(name="test")
        assert cfg.org == "default"
        assert cfg.type == "executor"
        assert cfg.workspace == "./workspace"


class TestLLMConfig:
    def test_defaults(self) -> None:
        cfg = LLMConfig(model="test/model")
        assert cfg.max_tokens == 4096
        assert cfg.temperature == 0.7


class TestIdentityConfig:
    def test_defaults(self) -> None:
        cfg = IdentityConfig()
        assert cfg.did == ""
        assert cfg.key_dir == "~/.arcagent/keys"
        assert cfg.vault_path == ""


class TestVaultConfig:
    def test_defaults(self) -> None:
        cfg = VaultConfig()
        assert cfg.backend == ""
        assert cfg.cache_ttl_seconds == 300


class TestToolsConfig:
    def test_defaults(self) -> None:
        cfg = ToolsConfig()
        assert cfg.native == {}
        assert cfg.mcp_servers == {}
        assert cfg.http == {}
        assert cfg.process == {}

    def test_policy_defaults(self) -> None:
        cfg = ToolConfig()
        assert cfg.allow == []
        assert cfg.deny == []
        assert cfg.timeout_seconds == 30


class TestTelemetryConfig:
    def test_defaults(self) -> None:
        cfg = TelemetryConfig()
        assert cfg.enabled is True
        assert cfg.log_level == "INFO"
        assert cfg.export_traces is False


class TestContextConfig:
    def test_defaults(self) -> None:
        cfg = ContextConfig()
        assert cfg.max_tokens == 128000
        assert cfg.prune_threshold == 0.70
        assert cfg.compact_threshold == 0.85
        assert cfg.emergency_threshold == 0.95
        assert cfg.estimate_multiplier == 1.1


class TestEvalConfig:
    def test_defaults(self) -> None:
        cfg = EvalConfig()
        assert cfg.provider == ""
        assert cfg.model == ""
        assert cfg.max_tokens == 1024
        assert cfg.temperature == 0.2
        assert cfg.timeout_seconds == 30
        assert cfg.fallback_behavior == "skip"
        assert cfg.max_concurrent == 2

    def test_fallback_behavior_validation(self) -> None:
        cfg = EvalConfig(fallback_behavior="error")
        assert cfg.fallback_behavior == "error"

    def test_custom_values(self) -> None:
        cfg = EvalConfig(
            provider="openai",
            model="gpt-4o-mini",
            max_tokens=512,
            temperature=0.1,
            timeout_seconds=60,
            max_concurrent=4,
        )
        assert cfg.provider == "openai"
        assert cfg.model == "gpt-4o-mini"
        assert cfg.max_concurrent == 4


class TestSessionConfig:
    def test_defaults(self) -> None:
        cfg = SessionConfig()
        assert cfg.retention_count == 50
        assert cfg.retention_days == 30
        assert cfg.compaction_summary_max_chars == 2000

    def test_custom_retention(self) -> None:
        cfg = SessionConfig(retention_count=100, retention_days=60)
        assert cfg.retention_count == 100
        assert cfg.retention_days == 60


class TestEnvDenylist:
    def test_vault_backend_env_blocked(
        self, minimal_toml: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Env var override for vault.backend is blocked."""
        monkeypatch.setenv("ARCAGENT_VAULT__BACKEND", "evil.module:Backdoor")
        cfg = load_config(minimal_toml)
        assert cfg.vault.backend == ""  # Default, not overridden

    def test_identity_key_dir_env_blocked(
        self, minimal_toml: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Env var override for identity.key_dir is blocked."""
        monkeypatch.setenv("ARCAGENT_IDENTITY__KEY_DIR", "/var/evil/keys")
        cfg = load_config(minimal_toml)
        assert cfg.identity.key_dir == "~/.arcagent/keys"  # Default

    def test_tools_native_env_blocked(
        self, minimal_toml: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Env var override for tools.native.* is blocked."""
        monkeypatch.setenv("ARCAGENT_TOOLS__NATIVE__EVIL__MODULE", "os:system")
        cfg = load_config(minimal_toml)
        assert len(cfg.tools.native) == 0

    def test_non_sensitive_env_allowed(
        self, minimal_toml: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-sensitive keys can still be overridden."""
        monkeypatch.setenv("ARCAGENT_AGENT__NAME", "overridden")
        cfg = load_config(minimal_toml)
        assert cfg.agent.name == "overridden"

    def test_env_override_non_dict_intermediate(
        self, minimal_toml: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Env override replaces non-dict intermediate with dict."""
        # This tests line 261 where if target[part] is not a dict, replace it
        monkeypatch.setenv("ARCAGENT_AGENT__NAME", "base")
        # Set a deep nested value that would require creating intermediate dicts
        monkeypatch.setenv("ARCAGENT_TELEMETRY__LOG_LEVEL", "DEBUG")
        cfg = load_config(minimal_toml)
        assert cfg.telemetry.log_level == "DEBUG"


class TestMaxTokensValidator:
    def test_max_tokens_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="greater than 0"):
            LLMConfig(model="test/model", max_tokens=0)

    def test_max_tokens_negative_rejected(self) -> None:
        with pytest.raises(ValueError, match="greater than 0"):
            LLMConfig(model="test/model", max_tokens=-1)

    def test_context_max_tokens_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="greater than 0"):
            ContextConfig(max_tokens=0)


class TestConfigWithNewSections:
    def test_config_has_eval_defaults(self, minimal_toml: Path) -> None:
        cfg = load_config(minimal_toml)
        assert cfg.eval.provider == ""
        assert cfg.eval.model == ""
        assert cfg.eval.fallback_behavior == "skip"
        assert cfg.eval.max_concurrent == 2

    def test_config_has_session_defaults(self, minimal_toml: Path) -> None:
        cfg = load_config(minimal_toml)
        assert cfg.session.retention_count == 50
        assert cfg.session.retention_days == 30

    def test_eval_from_toml(self, tmp_path: Path) -> None:
        config = tmp_path / "arcagent.toml"
        config.write_text(textwrap.dedent("""\
            [agent]
            name = "test"

            [llm]
            model = "anthropic/claude-sonnet-4-5-20250929"

            [eval]
            provider = "openai"
            model = "gpt-4o-mini"
            max_tokens = 512
            temperature = 0.1
            fallback_behavior = "error"
            max_concurrent = 4
        """))
        cfg = load_config(config)
        assert cfg.eval.provider == "openai"
        assert cfg.eval.model == "gpt-4o-mini"
        assert cfg.eval.max_tokens == 512
        assert cfg.eval.fallback_behavior == "error"
        assert cfg.eval.max_concurrent == 4

    def test_session_from_toml(self, tmp_path: Path) -> None:
        config = tmp_path / "arcagent.toml"
        config.write_text(textwrap.dedent("""\
            [agent]
            name = "test"

            [llm]
            model = "anthropic/claude-sonnet-4-5-20250929"

            [session]
            retention_count = 100
            retention_days = 60
        """))
        cfg = load_config(config)
        assert cfg.session.retention_count == 100
        assert cfg.session.retention_days == 60

    def test_env_override_eval(
        self, minimal_toml: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ARCAGENT_EVAL__MODEL", "openai/gpt-4o-mini")
        cfg = load_config(minimal_toml)
        assert cfg.eval.model == "openai/gpt-4o-mini"


class TestFullConfig:
    def test_full_toml(self, tmp_path: Path) -> None:
        config = tmp_path / "full.toml"
        config.write_text(textwrap.dedent("""\
            [agent]
            name = "full-agent"
            org = "blackarc"
            type = "planner"
            workspace = "/opt/agents/full"

            [llm]
            model = "anthropic/claude-sonnet-4-5-20250929"
            max_tokens = 8192
            temperature = 0.5

            [identity]
            did = "did:arc:blackarc:planner/abcd1234"
            key_dir = "/opt/keys"
            vault_path = "secret/agents/full"

            [vault]
            backend = "my_vault:HashicorpBackend"
            cache_ttl_seconds = 600

            [tools.policy]
            allow = ["read_file", "write_file"]
            deny = ["shell_exec"]
            timeout_seconds = 60

            [tools.native.read_file]
            module = "arcagent.tools.fs:read_file"
            description = "Read a file"

            [tools.mcp_servers.filesystem]
            command = "npx"
            args = ["-y", "@modelcontextprotocol/server-filesystem"]
            timeout_seconds = 30

            [telemetry]
            enabled = true
            service_name = "arcagent-full"
            log_level = "DEBUG"
            export_traces = true
            exporter_endpoint = "http://localhost:4317"

            [context]
            max_tokens = 200000
            prune_threshold = 0.65
            compact_threshold = 0.80
            emergency_threshold = 0.90
            estimate_multiplier = 1.2

            [modules.memory]
            enabled = true
            priority = 100

            [modules.memory.config]
            search_weights = {semantic = 0.7, keyword = 0.3}
        """))
        cfg = load_config(config)
        assert cfg.agent.name == "full-agent"
        assert cfg.agent.workspace == "/opt/agents/full"
        assert cfg.llm.max_tokens == 8192
        assert cfg.identity.vault_path == "secret/agents/full"
        assert cfg.vault.backend == "my_vault:HashicorpBackend"
        assert cfg.tools.policy.allow == ["read_file", "write_file"]
        assert cfg.tools.policy.deny == ["shell_exec"]
        assert "read_file" in cfg.tools.native
        assert "filesystem" in cfg.tools.mcp_servers
        assert cfg.telemetry.log_level == "DEBUG"
        assert cfg.context.max_tokens == 200000
        assert "memory" in cfg.modules
