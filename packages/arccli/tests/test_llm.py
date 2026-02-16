"""Tests for arc llm subcommands."""

import json
import sys
from unittest.mock import AsyncMock, patch

from click.testing import CliRunner

from arccli.main import cli
from arcllm.types import LLMResponse, Usage


runner = CliRunner()


def _mock_response(content: str = "Hello!", model: str = "test-model") -> LLMResponse:
    """Create a mock LLMResponse for testing."""
    return LLMResponse(
        content=content,
        tool_calls=[],
        usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
        model=model,
        stop_reason="end_turn",
    )


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------


class TestVersion:
    def test_version_shows_output(self):
        result = runner.invoke(cli, ["llm", "version"])
        assert result.exit_code == 0
        assert "arcllm" in result.output
        assert "arccli" in result.output

    def test_version_shows_python(self):
        result = runner.invoke(cli, ["llm", "version"])
        assert result.exit_code == 0
        assert "python" in result.output.lower()

    def test_version_json(self):
        result = runner.invoke(cli, ["llm", "version", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "arcllm" in data
        assert "arccli" in data
        assert "python" in data


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


class TestConfig:
    def test_config_shows_defaults(self):
        result = runner.invoke(cli, ["llm", "config"])
        assert result.exit_code == 0
        assert "defaults" in result.output.lower()
        assert "provider" in result.output.lower()

    def test_config_shows_modules(self):
        result = runner.invoke(cli, ["llm", "config"])
        assert result.exit_code == 0
        assert "modules" in result.output.lower()

    def test_config_module_filter(self):
        result = runner.invoke(cli, ["llm", "config", "--module", "telemetry"])
        assert result.exit_code == 0
        assert "telemetry" in result.output.lower()

    def test_config_module_unknown(self):
        result = runner.invoke(cli, ["llm", "config", "--module", "nonexistent"])
        assert result.exit_code != 0

    def test_config_json(self):
        result = runner.invoke(cli, ["llm", "config", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "defaults" in data
        assert "modules" in data


# ---------------------------------------------------------------------------
# providers
# ---------------------------------------------------------------------------


class TestProviders:
    def test_providers_lists_table(self):
        result = runner.invoke(cli, ["llm", "providers"])
        assert result.exit_code == 0
        assert "anthropic" in result.output.lower()

    def test_providers_has_columns(self):
        result = runner.invoke(cli, ["llm", "providers"])
        assert result.exit_code == 0
        assert "Name" in result.output
        assert "Default Model" in result.output

    def test_providers_json(self):
        result = runner.invoke(cli, ["llm", "providers", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) > 0
        assert "name" in data[0]


# ---------------------------------------------------------------------------
# provider <name>
# ---------------------------------------------------------------------------


class TestProvider:
    def test_provider_anthropic(self):
        result = runner.invoke(cli, ["llm", "provider", "anthropic"])
        assert result.exit_code == 0
        assert "anthropic" in result.output.lower()
        assert "claude" in result.output.lower()

    def test_provider_shows_models(self):
        result = runner.invoke(cli, ["llm", "provider", "anthropic"])
        assert result.exit_code == 0
        assert "context" in result.output.lower() or "Context" in result.output

    def test_provider_unknown(self):
        result = runner.invoke(cli, ["llm", "provider", "nonexistent"])
        assert result.exit_code != 0
        assert "not found" in result.output.lower() or "error" in result.output.lower()

    def test_provider_json(self):
        result = runner.invoke(cli, ["llm", "provider", "anthropic", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "provider" in data
        assert "models" in data


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------


class TestModels:
    def test_models_lists_all(self):
        result = runner.invoke(cli, ["llm", "models"])
        assert result.exit_code == 0
        assert "anthropic" in result.output.lower()

    def test_models_provider_filter(self):
        result = runner.invoke(cli, ["llm", "models", "--provider", "anthropic"])
        assert result.exit_code == 0
        assert "claude" in result.output.lower()

    def test_models_tools_filter(self):
        result = runner.invoke(cli, ["llm", "models", "--tools"])
        assert result.exit_code == 0

    def test_models_vision_filter(self):
        result = runner.invoke(cli, ["llm", "models", "--vision"])
        assert result.exit_code == 0

    def test_models_json(self):
        result = runner.invoke(cli, ["llm", "models", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        if len(data) > 0:
            assert "provider" in data[0]
            assert "model" in data[0]


# ---------------------------------------------------------------------------
# call
# ---------------------------------------------------------------------------


class TestCall:
    @patch("arccli.llm.load_model")
    def test_call_basic(self, mock_load):
        adapter = AsyncMock()
        adapter.invoke.return_value = _mock_response()
        adapter.close = AsyncMock()
        mock_load.return_value = adapter

        result = runner.invoke(cli, ["llm", "call", "anthropic", "Hi"])
        assert result.exit_code == 0
        assert "Hello!" in result.output

    @patch("arccli.llm.load_model")
    def test_call_json(self, mock_load):
        adapter = AsyncMock()
        adapter.invoke.return_value = _mock_response()
        adapter.close = AsyncMock()
        mock_load.return_value = adapter

        result = runner.invoke(cli, ["llm", "call", "anthropic", "Hi", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["content"] == "Hello!"
        assert data["model"] == "test-model"
        assert "usage" in data

    @patch("arccli.llm.load_model")
    def test_call_with_model_flag(self, mock_load):
        adapter = AsyncMock()
        adapter.invoke.return_value = _mock_response()
        adapter.close = AsyncMock()
        mock_load.return_value = adapter

        result = runner.invoke(cli, ["llm", "call", "anthropic", "Hi", "--model", "claude-haiku"])
        assert result.exit_code == 0
        mock_load.assert_called_once()
        call_kwargs = mock_load.call_args
        assert call_kwargs[1].get("model") == "claude-haiku" or call_kwargs[0][1] == "claude-haiku"

    @patch("arccli.llm.load_model")
    def test_call_with_system(self, mock_load):
        adapter = AsyncMock()
        adapter.invoke.return_value = _mock_response()
        adapter.close = AsyncMock()
        mock_load.return_value = adapter

        result = runner.invoke(cli, ["llm", "call", "anthropic", "Hi", "--system", "Be helpful"])
        assert result.exit_code == 0
        # Verify invoke was called with system message
        invoke_args = adapter.invoke.call_args[0][0]  # first positional arg (messages)
        assert any(m.role == "system" for m in invoke_args)

    @patch("arccli.llm.load_model")
    def test_call_with_temperature(self, mock_load):
        adapter = AsyncMock()
        adapter.invoke.return_value = _mock_response()
        adapter.close = AsyncMock()
        mock_load.return_value = adapter

        result = runner.invoke(cli, ["llm", "call", "anthropic", "Hi", "--temperature", "0.5"])
        assert result.exit_code == 0

    @patch("arccli.llm.load_model")
    def test_call_module_retry_flag(self, mock_load):
        adapter = AsyncMock()
        adapter.invoke.return_value = _mock_response()
        adapter.close = AsyncMock()
        mock_load.return_value = adapter

        result = runner.invoke(cli, ["llm", "call", "anthropic", "Hi", "--retry"])
        assert result.exit_code == 0
        _, kwargs = mock_load.call_args
        assert kwargs["retry"] is True

    @patch("arccli.llm.load_model")
    def test_call_module_no_retry_flag(self, mock_load):
        adapter = AsyncMock()
        adapter.invoke.return_value = _mock_response()
        adapter.close = AsyncMock()
        mock_load.return_value = adapter

        result = runner.invoke(cli, ["llm", "call", "anthropic", "Hi", "--no-retry"])
        assert result.exit_code == 0
        _, kwargs = mock_load.call_args
        assert kwargs["retry"] is False

    @patch("arccli.llm.load_model")
    def test_call_module_fallback_flag(self, mock_load):
        adapter = AsyncMock()
        adapter.invoke.return_value = _mock_response()
        adapter.close = AsyncMock()
        mock_load.return_value = adapter

        result = runner.invoke(cli, ["llm", "call", "anthropic", "Hi", "--fallback"])
        assert result.exit_code == 0
        _, kwargs = mock_load.call_args
        assert kwargs["fallback"] is True

    @patch("arccli.llm.load_model")
    def test_call_module_no_fallback_flag(self, mock_load):
        adapter = AsyncMock()
        adapter.invoke.return_value = _mock_response()
        adapter.close = AsyncMock()
        mock_load.return_value = adapter

        result = runner.invoke(cli, ["llm", "call", "anthropic", "Hi", "--no-fallback"])
        assert result.exit_code == 0
        _, kwargs = mock_load.call_args
        assert kwargs["fallback"] is False

    @patch("arccli.llm.load_model")
    def test_call_module_rate_limit_flag(self, mock_load):
        adapter = AsyncMock()
        adapter.invoke.return_value = _mock_response()
        adapter.close = AsyncMock()
        mock_load.return_value = adapter

        result = runner.invoke(cli, ["llm", "call", "anthropic", "Hi", "--rate-limit"])
        assert result.exit_code == 0
        _, kwargs = mock_load.call_args
        assert kwargs["rate_limit"] is True

    @patch("arccli.llm.load_model")
    def test_call_module_telemetry_flag(self, mock_load):
        adapter = AsyncMock()
        adapter.invoke.return_value = _mock_response()
        adapter.close = AsyncMock()
        mock_load.return_value = adapter

        result = runner.invoke(cli, ["llm", "call", "anthropic", "Hi", "--telemetry"])
        assert result.exit_code == 0
        _, kwargs = mock_load.call_args
        assert kwargs["telemetry"] is True

    @patch("arccli.llm.load_model")
    def test_call_module_no_telemetry_flag(self, mock_load):
        adapter = AsyncMock()
        adapter.invoke.return_value = _mock_response()
        adapter.close = AsyncMock()
        mock_load.return_value = adapter

        result = runner.invoke(cli, ["llm", "call", "anthropic", "Hi", "--no-telemetry"])
        assert result.exit_code == 0
        _, kwargs = mock_load.call_args
        assert kwargs["telemetry"] is False

    @patch("arccli.llm.load_model")
    def test_call_module_audit_flag(self, mock_load):
        adapter = AsyncMock()
        adapter.invoke.return_value = _mock_response()
        adapter.close = AsyncMock()
        mock_load.return_value = adapter

        result = runner.invoke(cli, ["llm", "call", "anthropic", "Hi", "--audit"])
        assert result.exit_code == 0
        _, kwargs = mock_load.call_args
        assert kwargs["audit"] is True

    @patch("arccli.llm.load_model")
    def test_call_module_security_flag(self, mock_load):
        adapter = AsyncMock()
        adapter.invoke.return_value = _mock_response()
        adapter.close = AsyncMock()
        mock_load.return_value = adapter

        result = runner.invoke(cli, ["llm", "call", "anthropic", "Hi", "--security"])
        assert result.exit_code == 0
        _, kwargs = mock_load.call_args
        assert kwargs["security"] is True

    @patch("arccli.llm.load_model")
    def test_call_module_otel_flag(self, mock_load):
        adapter = AsyncMock()
        adapter.invoke.return_value = _mock_response()
        adapter.close = AsyncMock()
        mock_load.return_value = adapter

        result = runner.invoke(cli, ["llm", "call", "anthropic", "Hi", "--otel"])
        assert result.exit_code == 0
        _, kwargs = mock_load.call_args
        assert kwargs["otel"] is True

    @patch("arccli.llm.load_model")
    def test_call_module_no_otel_flag(self, mock_load):
        adapter = AsyncMock()
        adapter.invoke.return_value = _mock_response()
        adapter.close = AsyncMock()
        mock_load.return_value = adapter

        result = runner.invoke(cli, ["llm", "call", "anthropic", "Hi", "--no-otel"])
        assert result.exit_code == 0
        _, kwargs = mock_load.call_args
        assert kwargs["otel"] is False

    @patch("arccli.llm.load_model")
    def test_call_verbose(self, mock_load):
        adapter = AsyncMock()
        adapter.invoke.return_value = _mock_response()
        adapter.close = AsyncMock()
        mock_load.return_value = adapter

        result = runner.invoke(cli, ["llm", "call", "anthropic", "Hi", "--verbose"])
        assert result.exit_code == 0
        assert "token" in result.output.lower() or "usage" in result.output.lower()

    @patch("arccli.llm.load_model")
    def test_call_error_handling(self, mock_load):
        mock_load.side_effect = Exception("API key missing")
        result = runner.invoke(cli, ["llm", "call", "anthropic", "Hi"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


class TestValidate:
    def test_validate_runs(self):
        result = runner.invoke(cli, ["llm", "validate"])
        assert result.exit_code == 0
        assert "anthropic" in result.output.lower()

    def test_validate_shows_status(self):
        result = runner.invoke(cli, ["llm", "validate"])
        assert result.exit_code == 0
        # Should show some form of pass/fail indicator
        output_lower = result.output.lower()
        assert "ok" in output_lower or "pass" in output_lower or "yes" in output_lower or "valid" in output_lower

    def test_validate_provider_filter(self):
        result = runner.invoke(cli, ["llm", "validate", "--provider", "anthropic"])
        assert result.exit_code == 0
        assert "anthropic" in result.output.lower()

    def test_validate_json(self):
        result = runner.invoke(cli, ["llm", "validate", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        if len(data) > 0:
            assert "provider" in data[0]
            assert "config_valid" in data[0]
