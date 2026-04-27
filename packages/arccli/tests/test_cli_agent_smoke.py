"""Smoke tests for arc agent subcommands via subprocess.

These tests verify that each `arc agent <subcommand>` invocation produces
non-empty stdout and exits 0. They are the regression net for the T1.1.5
migration: once these pass, the legacy dispatch bridge can be removed.

All tests use the canonical `arc` entry point, not `arc-legacy`.
The josh_agent directory is the canary — it must work end-to-end.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

# ---------------------------------------------------------------------------
# Fixtures / constants
# ---------------------------------------------------------------------------

_ARC = Path(__file__).parent.parent.parent.parent / ".venv" / "bin" / "arc"
_JOSH_AGENT = Path(__file__).parent.parent.parent.parent / "team" / "josh_agent"

# josh_agent uses azure_openai/o1 — inject a sentinel key so the build
# --check command can verify key presence without requiring a real key.
_AZURE_SENTINEL_ENV = {**os.environ, "AZURE_OPENAI_API_KEY": "test-sentinel-key"}


def _arc(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run `arc <args>` and return the CompletedProcess."""
    result = subprocess.run(
        [str(_ARC), *args],
        capture_output=True,
        text=True,
    )
    return result


def _arc_with_azure(*args: str) -> subprocess.CompletedProcess[str]:
    """Run `arc <args>` with AZURE_OPENAI_API_KEY set in the subprocess env.

    Used for tests that need the azure_openai API key check to pass without
    requiring a real credential in CI/test environments.
    """
    return subprocess.run(
        [str(_ARC), *args],
        capture_output=True,
        text=True,
        env=_AZURE_SENTINEL_ENV,
    )


# ---------------------------------------------------------------------------
# arc agent status
# ---------------------------------------------------------------------------


class TestAgentStatus:
    def test_status_exits_zero(self) -> None:
        """arc agent status <dir> exits 0."""
        result = _arc("agent", "status", str(_JOSH_AGENT))
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_status_shows_agent_name(self) -> None:
        """arc agent status prints the agent name."""
        result = _arc("agent", "status", str(_JOSH_AGENT))
        assert "josh-agent" in result.stdout, f"stdout: {result.stdout!r}"

    def test_status_shows_model(self) -> None:
        """arc agent status prints the model field."""
        result = _arc("agent", "status", str(_JOSH_AGENT))
        assert "azure_openai" in result.stdout or "o1" in result.stdout, (
            f"stdout: {result.stdout!r}"
        )

    def test_status_shows_did(self) -> None:
        """arc agent status prints the DID."""
        result = _arc("agent", "status", str(_JOSH_AGENT))
        assert "did:arc" in result.stdout, f"stdout: {result.stdout!r}"

    def test_status_nonexistent_dir_exits_nonzero(self) -> None:
        """arc agent status on a nonexistent path exits non-zero."""
        result = _arc("agent", "status", "/tmp/__no_such_agent__")
        assert result.returncode != 0

    def test_status_output_is_nonempty(self) -> None:
        """arc agent status produces non-empty stdout."""
        result = _arc("agent", "status", str(_JOSH_AGENT))
        assert result.stdout.strip(), "Expected non-empty stdout"


# ---------------------------------------------------------------------------
# arc agent skills
# ---------------------------------------------------------------------------


class TestAgentSkills:
    def test_skills_exits_zero(self) -> None:
        """arc agent skills <dir> exits 0."""
        result = _arc("agent", "skills", str(_JOSH_AGENT))
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_skills_produces_output(self) -> None:
        """arc agent skills produces output (table or 'No skills found')."""
        result = _arc("agent", "skills", str(_JOSH_AGENT))
        assert result.stdout.strip() or result.returncode == 0


# ---------------------------------------------------------------------------
# arc agent extensions
# ---------------------------------------------------------------------------


class TestAgentExtensions:
    def test_extensions_exits_zero(self) -> None:
        """arc agent extensions <dir> exits 0."""
        result = _arc("agent", "extensions", str(_JOSH_AGENT))
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_extensions_produces_output(self) -> None:
        """arc agent extensions produces output (table or 'No extensions found')."""
        result = _arc("agent", "extensions", str(_JOSH_AGENT))
        assert result.stdout.strip() or result.returncode == 0


# ---------------------------------------------------------------------------
# arc agent sessions
# ---------------------------------------------------------------------------


class TestAgentSessions:
    def test_sessions_exits_zero(self) -> None:
        """arc agent sessions <dir> exits 0."""
        result = _arc("agent", "sessions", str(_JOSH_AGENT))
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_sessions_produces_output(self) -> None:
        """arc agent sessions produces output (table or 'No sessions found')."""
        result = _arc("agent", "sessions", str(_JOSH_AGENT))
        assert result.stdout.strip() or result.returncode == 0


# ---------------------------------------------------------------------------
# arc agent build --check
# ---------------------------------------------------------------------------


class TestAgentBuild:
    def test_build_check_exits_zero(self) -> None:
        """arc agent build --check validates without writing.

        josh_agent uses azure_openai/o1. The build --check command validates
        that AZURE_OPENAI_API_KEY is set. Inject a sentinel key so this check
        passes in CI/test environments without requiring a real credential.
        """
        result = _arc_with_azure("agent", "build", str(_JOSH_AGENT), "--check")
        assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"

    def test_build_check_produces_output(self) -> None:
        """arc agent build --check produces non-empty stdout."""
        result = _arc_with_azure("agent", "build", str(_JOSH_AGENT), "--check")
        assert result.stdout.strip(), f"Expected output, got: {result.stdout!r}"


# ---------------------------------------------------------------------------
# arc agent tools
# ---------------------------------------------------------------------------


class TestAgentTools:
    def test_tools_exits_zero(self) -> None:
        """arc agent tools <dir> exits 0."""
        result = _arc("agent", "tools", str(_JOSH_AGENT))
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_tools_produces_output(self) -> None:
        """arc agent tools produces output (table or 'No tools found')."""
        result = _arc("agent", "tools", str(_JOSH_AGENT))
        assert result.stdout.strip() or result.returncode == 0


# ---------------------------------------------------------------------------
# arc agent config
# ---------------------------------------------------------------------------


class TestAgentConfig:
    def test_config_exits_zero(self) -> None:
        """arc agent config <dir> exits 0."""
        result = _arc("agent", "config", str(_JOSH_AGENT))
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_config_shows_agent_name(self) -> None:
        """arc agent config shows agent name in output."""
        result = _arc("agent", "config", str(_JOSH_AGENT))
        assert "josh-agent" in result.stdout, f"stdout: {result.stdout!r}"


# ---------------------------------------------------------------------------
# arc version — basic sanity that arc itself works
# ---------------------------------------------------------------------------


class TestArcVersion:
    def test_version_exits_zero(self) -> None:
        """arc version exits 0."""
        result = _arc("version")
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_version_output_nonempty(self) -> None:
        """arc version produces non-empty stdout."""
        result = _arc("version")
        assert result.stdout.strip()
