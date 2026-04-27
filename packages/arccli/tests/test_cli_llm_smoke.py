"""Smoke tests for arc llm subcommands via subprocess.

These tests verify that each `arc llm <subcommand>` invocation produces
non-empty stdout and exits 0. They are the regression net for the T1.1.5
migration: once these pass, the legacy dispatch bridge can be removed.

All tests use the canonical `arc` entry point, not `arc-legacy`.
No real LLM calls are made — only metadata/config subcommands are exercised.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

_ARC = Path(__file__).parent.parent.parent.parent / ".venv" / "bin" / "arc"


def _arc(*args: str) -> subprocess.CompletedProcess[str]:
    """Run `arc <args>` and return the CompletedProcess."""
    return subprocess.run(
        [str(_ARC), *args],
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# arc llm (no subcommand — shows help)
# ---------------------------------------------------------------------------


class TestLlmHelp:
    def test_no_args_exits_zero(self) -> None:
        """arc llm with no args exits 0 and shows help."""
        result = _arc("llm")
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_no_args_shows_subcommands(self) -> None:
        """arc llm help lists subcommands."""
        result = _arc("llm")
        assert result.stdout.strip(), "Expected non-empty stdout"
        combined = result.stdout + result.stderr
        # At least one expected subcommand must appear
        assert any(
            sub in combined for sub in ["version", "config", "providers", "models", "validate"]
        )


# ---------------------------------------------------------------------------
# arc llm version
# ---------------------------------------------------------------------------


class TestLlmVersion:
    def test_version_exits_zero(self) -> None:
        """arc llm version exits 0."""
        result = _arc("llm", "version")
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_version_output_nonempty(self) -> None:
        """arc llm version produces non-empty stdout."""
        result = _arc("llm", "version")
        assert result.stdout.strip()

    def test_version_shows_arcllm(self) -> None:
        """arc llm version shows arcllm version."""
        result = _arc("llm", "version")
        assert "arcllm" in result.stdout

    def test_version_json(self) -> None:
        """arc llm version --json produces valid JSON."""
        result = _arc("llm", "version", "--json")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        data = json.loads(result.stdout)
        assert "arcllm" in data
        assert "arccmd" in data
        assert "python" in data


# ---------------------------------------------------------------------------
# arc llm config
# ---------------------------------------------------------------------------


class TestLlmConfig:
    def test_config_exits_zero(self) -> None:
        """arc llm config exits 0."""
        result = _arc("llm", "config")
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_config_output_nonempty(self) -> None:
        """arc llm config produces non-empty stdout."""
        result = _arc("llm", "config")
        assert result.stdout.strip()

    def test_config_shows_defaults(self) -> None:
        """arc llm config shows defaults section."""
        result = _arc("llm", "config")
        assert "defaults" in result.stdout.lower()

    def test_config_json(self) -> None:
        """arc llm config --json produces valid JSON with expected keys."""
        result = _arc("llm", "config", "--json")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        data = json.loads(result.stdout)
        assert "defaults" in data
        assert "modules" in data

    def test_config_module_filter(self) -> None:
        """arc llm config --module telemetry shows telemetry section."""
        result = _arc("llm", "config", "--module", "telemetry")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "telemetry" in result.stdout.lower()

    def test_config_unknown_module_fails(self) -> None:
        """arc llm config --module nonexistent exits non-zero."""
        result = _arc("llm", "config", "--module", "nonexistent_xyz_abc")
        assert result.returncode != 0


# ---------------------------------------------------------------------------
# arc llm providers
# ---------------------------------------------------------------------------


class TestLlmProviders:
    def test_providers_exits_zero(self) -> None:
        """arc llm providers exits 0."""
        result = _arc("llm", "providers")
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_providers_output_nonempty(self) -> None:
        """arc llm providers produces non-empty stdout."""
        result = _arc("llm", "providers")
        assert result.stdout.strip()

    def test_providers_lists_anthropic(self) -> None:
        """arc llm providers lists anthropic provider."""
        result = _arc("llm", "providers")
        assert "anthropic" in result.stdout.lower()

    def test_providers_json(self) -> None:
        """arc llm providers --json produces valid JSON list."""
        result = _arc("llm", "providers", "--json")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        data = json.loads(result.stdout)
        assert isinstance(data, list)
        assert len(data) > 0
        assert "name" in data[0]


# ---------------------------------------------------------------------------
# arc llm provider <name>
# ---------------------------------------------------------------------------


class TestLlmProvider:
    def test_provider_anthropic_exits_zero(self) -> None:
        """arc llm provider anthropic exits 0."""
        result = _arc("llm", "provider", "anthropic")
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_provider_shows_models(self) -> None:
        """arc llm provider anthropic shows model info."""
        result = _arc("llm", "provider", "anthropic")
        assert "claude" in result.stdout.lower()

    def test_provider_unknown_fails(self) -> None:
        """arc llm provider nonexistent exits non-zero."""
        result = _arc("llm", "provider", "nonexistent_xyz_abc")
        assert result.returncode != 0

    def test_provider_json(self) -> None:
        """arc llm provider anthropic --json produces valid JSON."""
        result = _arc("llm", "provider", "anthropic", "--json")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        data = json.loads(result.stdout)
        assert "provider" in data
        assert "models" in data


# ---------------------------------------------------------------------------
# arc llm models
# ---------------------------------------------------------------------------


class TestLlmModels:
    def test_models_exits_zero(self) -> None:
        """arc llm models exits 0."""
        result = _arc("llm", "models")
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_models_output_nonempty(self) -> None:
        """arc llm models produces non-empty stdout."""
        result = _arc("llm", "models")
        assert result.stdout.strip()

    def test_models_provider_filter(self) -> None:
        """arc llm models --provider anthropic shows only anthropic models."""
        result = _arc("llm", "models", "--provider", "anthropic")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "claude" in result.stdout.lower()

    def test_models_json(self) -> None:
        """arc llm models --json produces valid JSON list."""
        result = _arc("llm", "models", "--json")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        data = json.loads(result.stdout)
        assert isinstance(data, list)


# ---------------------------------------------------------------------------
# arc llm validate
# ---------------------------------------------------------------------------


class TestLlmValidate:
    def test_validate_exits_zero(self) -> None:
        """arc llm validate exits 0."""
        result = _arc("llm", "validate")
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_validate_output_nonempty(self) -> None:
        """arc llm validate produces non-empty stdout."""
        result = _arc("llm", "validate")
        assert result.stdout.strip()

    def test_validate_shows_providers(self) -> None:
        """arc llm validate shows provider status."""
        result = _arc("llm", "validate")
        assert "anthropic" in result.stdout.lower()

    def test_validate_provider_filter(self) -> None:
        """arc llm validate --provider anthropic works."""
        result = _arc("llm", "validate", "--provider", "anthropic")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "anthropic" in result.stdout.lower()

    def test_validate_json(self) -> None:
        """arc llm validate --json produces valid JSON list."""
        result = _arc("llm", "validate", "--json")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        data = json.loads(result.stdout)
        assert isinstance(data, list)
        if len(data) > 0:
            assert "provider" in data[0]
            assert "config_valid" in data[0]


# Mark to avoid unused import warning
_ = pytest
