"""Tests for `arc agent create` command — subprocess-based (T1.1.5 migration)."""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
from pathlib import Path

import pytest

_ARC = Path(__file__).parent.parent.parent.parent / ".venv" / "bin" / "arc"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory, monkeypatch):
    """Isolate ~/.arc from these tests.

    `arc agent create` auto-registers with arcteam (FIX-1), which writes to
    `~/.arc/team/messages/registry/`. Without isolation every test run would
    pollute the user's real arcteam registry. This fixture redirects HOME to
    a per-test tmp dir; subprocesses inherit it.
    """
    fake_home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(fake_home))
    yield fake_home


def _arc(*args: str) -> subprocess.CompletedProcess[str]:
    """Run `arc <args>` and return the CompletedProcess.

    Inherits HOME from the test environment (isolated by the autouse fixture).
    """
    return subprocess.run(
        [str(_ARC), *args],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )


def _registry_dir(home: Path) -> Path:
    """Return the path arcteam writes registry entries to."""
    return home / ".arc" / "team" / "messages" / "registry"


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
            "security",
        ]
        for section in expected_sections:
            assert section in config, f"Missing config section: {section}"
        # Old SPEC-021-deprecated section must not reappear.
        assert "extensions" not in config, "[extensions] block should be gone (SPEC-021)"

    def test_create_config_uses_agent_name(self, tmp_path):
        _arc("agent", "create", "test-bot", "--dir", str(tmp_path))
        config_path = tmp_path / "test-bot" / "arcagent.toml"
        config = tomllib.loads(config_path.read_text())
        assert config["agent"]["name"] == "test-bot"
        assert config["telemetry"]["service_name"] == "test-bot"

    def test_create_workspace_structure(self, tmp_path):
        _arc("agent", "create", "my-agent", "--dir", str(tmp_path))
        agent_root = tmp_path / "my-agent"
        ws = agent_root / "workspace"
        expected_workspace_dirs = [
            "notes",
            "entities",
            ".capabilities",
            "sessions",
            "archive",
            "library",
            "library/scripts",
            "library/templates",
            "library/prompts",
            "library/data",
            "library/snippets",
        ]
        for subdir in expected_workspace_dirs:
            assert (ws / subdir).is_dir(), f"Missing workspace dir: {subdir}"
        # SPEC-021: per-agent capabilities live at the AGENT root, not in workspace
        assert (agent_root / "capabilities").is_dir(), "Missing per-agent capabilities/ dir"
        # Old SPEC-021-deprecated layout must not reappear.
        assert not (ws / "extensions").exists(), "workspace/extensions/ should be gone"
        assert not (ws / "skills").exists(), "workspace/skills/ should be gone"

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

    def test_create_calculator_capability(self, tmp_path):
        _arc("agent", "create", "my-agent", "--dir", str(tmp_path))
        calc = tmp_path / "my-agent" / "capabilities" / "calculator.py"
        assert calc.exists(), "calculator should be scaffolded into <agent>/capabilities/"
        content = calc.read_text()
        # SPEC-021 calls for a @tool decorator, not the legacy extension(api) factory.
        assert "@tool(" in content
        assert "async def calculate" in content
        assert "def extension(api)" not in content

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


# ---------------------------------------------------------------------------
# Auto-registration with arcteam (FIX-1)
# ---------------------------------------------------------------------------


class TestAutoRegister:
    """`arc agent create` auto-registers the new agent with arcteam.

    Without this, agents serve and emit traces correctly but stay invisible to
    arcui's trace dashboard until the user manually runs `arc team register`
    + `arc team backfill-workspaces --apply`. FIX-1 makes the common path
    (just create the agent) work end-to-end.

    Workspace path must be the workspace SUBDIRECTORY (`<agent>/workspace`),
    not the agent root — JSONLTraceStore appends `/traces` to it and the
    real traces live in `<agent>/workspace/traces/`.
    """

    def test_create_writes_registry_entry(self, tmp_path, isolated_home):
        result = _arc("agent", "create", "auto-reg-test", "--dir", str(tmp_path))
        assert result.returncode == 0, f"stderr: {result.stderr}"

        registry_file = _registry_dir(isolated_home) / "auto-reg-test.json"
        assert registry_file.exists(), (
            f"expected registry entry at {registry_file}; "
            f"created files in registry dir: "
            f"{list(_registry_dir(isolated_home).iterdir()) if _registry_dir(isolated_home).is_dir() else 'dir missing'}"
        )

    def test_registered_workspace_path_ends_in_workspace_subdir(self, tmp_path, isolated_home):
        _arc("agent", "create", "wp-test", "--dir", str(tmp_path))

        registry_file = _registry_dir(isolated_home) / "wp-test.json"
        data = json.loads(registry_file.read_text())
        wp = data.get("workspace_path", "")

        # Critical: must end in /workspace, NOT at the agent root.
        # JSONLTraceStore appends /traces to this path, and the real traces
        # live in <agent>/workspace/traces/.
        assert wp.endswith("/workspace"), (
            f"workspace_path must point at the 'workspace' subdirectory "
            f"(JSONLTraceStore appends '/traces' to it); got {wp!r}"
        )
        # Sanity: it should also include the agent name
        assert "wp-test" in wp

    def test_no_register_flag_skips_registration(self, tmp_path, isolated_home):
        result = _arc(
            "agent",
            "create",
            "skip-reg-test",
            "--dir",
            str(tmp_path),
            "--no-register",
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"

        registry_file = _registry_dir(isolated_home) / "skip-reg-test.json"
        assert not registry_file.exists(), (
            f"--no-register should not write a registry entry, but found {registry_file}"
        )

    def test_create_succeeds_even_if_already_registered(self, tmp_path, isolated_home):
        # First create + register
        _arc("agent", "create", "dup-test", "--dir", str(tmp_path))
        # Delete the agent dir and recreate with the same name — registry entry
        # already exists; second create should not crash on the registration.
        import shutil

        shutil.rmtree(tmp_path / "dup-test")
        result = _arc("agent", "create", "dup-test", "--dir", str(tmp_path))
        # Create itself succeeds (filesystem); registration is best-effort idempotent
        assert result.returncode == 0, f"stderr: {result.stderr}"
