"""Tests for `arc agent create` command — subprocess-based (T1.1.5 migration)."""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

_ARC = Path(__file__).parent.parent.parent.parent / ".venv" / "bin" / "arc"


def _arc(*args: str) -> subprocess.CompletedProcess[str]:
    """Run `arc <args>` and return the CompletedProcess."""
    return subprocess.run(
        [str(_ARC), *args],
        capture_output=True,
        text=True,
    )


class TestCreate:
    def test_create_makes_directory(self, tmp_path):
        result = _arc("agent", "create", "my-agent", "--dir", str(tmp_path))
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert (tmp_path / "my-agent").is_dir()

    def test_create_writes_config(self, tmp_path):
        _arc("agent", "create", "my-agent", "--dir", str(tmp_path))
        config_path = tmp_path / "my-agent" / "arcagent.toml"
        assert config_path.exists()
        config = tomllib.loads(config_path.read_text())
        assert isinstance(config, dict)

    def test_create_config_has_all_sections(self, tmp_path):
        _arc("agent", "create", "my-agent", "--dir", str(tmp_path))
        config_path = tmp_path / "my-agent" / "arcagent.toml"
        config = tomllib.loads(config_path.read_text())
        expected_sections = [
            "agent",
            "llm",
            "identity",
            "vault",
            "tools",
            "telemetry",
            "context",
            "eval",
            "modules",
            "session",
            "extensions",
        ]
        for section in expected_sections:
            assert section in config, f"Missing config section: {section}"

    def test_create_config_uses_agent_name(self, tmp_path):
        _arc("agent", "create", "test-bot", "--dir", str(tmp_path))
        config_path = tmp_path / "test-bot" / "arcagent.toml"
        config = tomllib.loads(config_path.read_text())
        assert config["agent"]["name"] == "test-bot"
        assert config["telemetry"]["service_name"] == "test-bot"

    def test_create_workspace_structure(self, tmp_path):
        _arc("agent", "create", "my-agent", "--dir", str(tmp_path))
        ws = tmp_path / "my-agent" / "workspace"
        expected_dirs = [
            "notes",
            "entities",
            "skills",
            "skills/_agent-created",
            "extensions",
            "sessions",
            "archive",
            "library",
            "library/scripts",
            "library/templates",
            "library/prompts",
            "library/data",
            "library/snippets",
        ]
        for subdir in expected_dirs:
            assert (ws / subdir).is_dir(), f"Missing workspace dir: {subdir}"

    def test_create_identity_file(self, tmp_path):
        _arc("agent", "create", "my-agent", "--dir", str(tmp_path))
        identity = tmp_path / "my-agent" / "workspace" / "identity.md"
        assert identity.exists()
        assert len(identity.read_text().strip()) > 0

    def test_create_policy_file(self, tmp_path):
        _arc("agent", "create", "my-agent", "--dir", str(tmp_path))
        policy = tmp_path / "my-agent" / "workspace" / "policy.md"
        assert policy.exists()
        assert len(policy.read_text().strip()) > 0

    def test_create_context_file(self, tmp_path):
        _arc("agent", "create", "my-agent", "--dir", str(tmp_path))
        context = tmp_path / "my-agent" / "workspace" / "context.md"
        assert context.exists()
        assert len(context.read_text().strip()) > 0

    def test_create_extension_calculator(self, tmp_path):
        _arc("agent", "create", "my-agent", "--dir", str(tmp_path))
        calc = tmp_path / "my-agent" / "workspace" / "extensions" / "calculator.py"
        assert calc.exists()
        content = calc.read_text()
        assert "def extension(api)" in content
        assert "calculate" in content

    def test_create_tools_init(self, tmp_path):
        _arc("agent", "create", "my-agent", "--dir", str(tmp_path))
        init_file = tmp_path / "my-agent" / "tools" / "__init__.py"
        assert init_file.exists()

    def test_create_fails_if_exists(self, tmp_path):
        (tmp_path / "my-agent").mkdir()
        result = _arc("agent", "create", "my-agent", "--dir", str(tmp_path))
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "already exists" in combined.lower()

    def test_create_custom_model(self, tmp_path):
        _arc("agent", "create", "my-agent", "--dir", str(tmp_path), "--model", "openai/gpt-4o")
        config_path = tmp_path / "my-agent" / "arcagent.toml"
        config = tomllib.loads(config_path.read_text())
        assert config["llm"]["model"] == "openai/gpt-4o"

    def test_create_custom_dir(self, tmp_path):
        custom_dir = tmp_path / "custom" / "nested"
        custom_dir.mkdir(parents=True)
        result = _arc("agent", "create", "my-agent", "--dir", str(custom_dir))
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert (custom_dir / "my-agent").is_dir()
        assert (custom_dir / "my-agent" / "arcagent.toml").exists()

    def test_create_output_shows_structure(self, tmp_path):
        result = _arc("agent", "create", "my-agent", "--dir", str(tmp_path))
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "my-agent/" in result.stdout or "my-agent" in result.stdout
        assert "arcagent.toml" in result.stdout
