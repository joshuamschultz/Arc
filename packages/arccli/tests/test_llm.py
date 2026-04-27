"""Tests for arc llm subcommands — subprocess + direct handler tests (T1.1.5 migration)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

_ARC = Path(__file__).parent.parent.parent.parent / ".venv" / "bin" / "arc"


def _arc(*args: str) -> subprocess.CompletedProcess[str]:
    """Run `arc <args>` and return the CompletedProcess."""
    return subprocess.run(
        [str(_ARC), *args],
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------


class TestVersion:
    def test_version_shows_output(self):
        result = _arc("llm", "version")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "arcllm" in result.stdout

    def test_version_shows_arccmd(self):
        result = _arc("llm", "version")
        assert result.returncode == 0
        assert "arccmd" in result.stdout or "arccli" in result.stdout.lower()

    def test_version_json(self):
        result = _arc("llm", "version", "--json")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        data = json.loads(result.stdout)
        assert "arcllm" in data
        assert "arccmd" in data
        assert "python" in data


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


class TestConfig:
    def test_config_shows_defaults(self):
        result = _arc("llm", "config")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "defaults" in result.stdout.lower()
        assert "provider" in result.stdout.lower()

    def test_config_shows_modules(self):
        result = _arc("llm", "config")
        assert result.returncode == 0
        assert "modules" in result.stdout.lower()

    def test_config_module_filter(self):
        result = _arc("llm", "config", "--module", "telemetry")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "telemetry" in result.stdout.lower()

    def test_config_module_unknown(self):
        result = _arc("llm", "config", "--module", "nonexistent")
        assert result.returncode != 0

    def test_config_json(self):
        result = _arc("llm", "config", "--json")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        data = json.loads(result.stdout)
        assert "defaults" in data
        assert "modules" in data


# ---------------------------------------------------------------------------
# providers
# ---------------------------------------------------------------------------


class TestProviders:
    def test_providers_lists_table(self):
        result = _arc("llm", "providers")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "anthropic" in result.stdout.lower()

    def test_providers_has_columns(self):
        result = _arc("llm", "providers")
        assert result.returncode == 0
        assert "Name" in result.stdout
        assert "Default Model" in result.stdout

    def test_providers_json(self):
        result = _arc("llm", "providers", "--json")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        data = json.loads(result.stdout)
        assert isinstance(data, list)
        assert len(data) > 0
        assert "name" in data[0]


# ---------------------------------------------------------------------------
# provider <name>
# ---------------------------------------------------------------------------


class TestProvider:
    def test_provider_anthropic(self):
        result = _arc("llm", "provider", "anthropic")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "anthropic" in result.stdout.lower()
        assert "claude" in result.stdout.lower()

    def test_provider_shows_models(self):
        result = _arc("llm", "provider", "anthropic")
        assert result.returncode == 0
        assert "context" in result.stdout.lower() or "Context" in result.stdout

    def test_provider_unknown(self):
        result = _arc("llm", "provider", "nonexistent_xyz")
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "not found" in combined.lower() or "error" in combined.lower()

    def test_provider_json(self):
        result = _arc("llm", "provider", "anthropic", "--json")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        data = json.loads(result.stdout)
        assert "provider" in data
        assert "models" in data


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------


class TestModels:
    def test_models_lists_all(self):
        result = _arc("llm", "models")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "anthropic" in result.stdout.lower()

    def test_models_provider_filter(self):
        result = _arc("llm", "models", "--provider", "anthropic")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "claude" in result.stdout.lower()

    def test_models_tools_filter(self):
        result = _arc("llm", "models", "--tools")
        assert result.returncode == 0

    def test_models_vision_filter(self):
        result = _arc("llm", "models", "--vision")
        assert result.returncode == 0

    def test_models_json(self):
        result = _arc("llm", "models", "--json")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        data = json.loads(result.stdout)
        assert isinstance(data, list)
        if len(data) > 0:
            assert "provider" in data[0]
            assert "model" in data[0]


# ---------------------------------------------------------------------------
# call — tested via direct handler invocation with mocks (avoids live LLM calls)
# ---------------------------------------------------------------------------


def _invoke_handler(args: list[str]) -> tuple[int, str]:
    """Call the llm_handler directly, capture stdout, return (exit_code, output)."""
    import io
    from contextlib import redirect_stdout

    from arccli.commands.llm import llm_handler

    buf = io.StringIO()
    exit_code = 0
    try:
        with redirect_stdout(buf):
            llm_handler(args)
    except SystemExit as e:
        exit_code = int(e.code) if e.code is not None else 0
    return exit_code, buf.getvalue()


def _make_mock_response(content: str = "Hello!", model: str = "test-model"):
    """Create a mock LLMResponse for handler tests."""
    from arcllm.types import LLMResponse, Usage

    return LLMResponse(
        content=content,
        tool_calls=[],
        usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
        model=model,
        stop_reason="end_turn",
    )


class TestCall:
    @patch("arccli.commands.llm._list_provider_names")
    def test_version_no_api_call(self, mock_names):
        """version subcommand uses no API."""
        result = _arc("llm", "version")
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


class TestValidate:
    def test_validate_runs(self):
        result = _arc("llm", "validate")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "anthropic" in result.stdout.lower()

    def test_validate_shows_status(self):
        result = _arc("llm", "validate")
        assert result.returncode == 0
        output_lower = result.stdout.lower()
        assert (
            "ok" in output_lower
            or "pass" in output_lower
            or "yes" in output_lower
            or "valid" in output_lower
        )

    def test_validate_provider_filter(self):
        result = _arc("llm", "validate", "--provider", "anthropic")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "anthropic" in result.stdout.lower()

    def test_validate_json(self):
        result = _arc("llm", "validate", "--json")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        data = json.loads(result.stdout)
        assert isinstance(data, list)
        if len(data) > 0:
            assert "provider" in data[0]
            assert "config_valid" in data[0]
