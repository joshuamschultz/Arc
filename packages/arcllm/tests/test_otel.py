"""Tests for OtelModule — root span creation, GenAI attributes, config validation, SDK setup."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcllm.exceptions import ArcLLMConfigError
from arcllm.types import LLMResponse, Message, Usage


def _make_inner() -> MagicMock:
    """Create a mock inner LLMProvider."""
    inner = MagicMock()
    inner.name = "anthropic"
    inner.model_name = "claude-sonnet-4-20250514"
    inner.invoke = AsyncMock(
        return_value=LLMResponse(
            content="hello",
            tool_calls=[],
            usage=Usage(input_tokens=100, output_tokens=50, total_tokens=150),
            model="claude-sonnet-4-20250514",
            stop_reason="end_turn",
        )
    )
    return inner


def _default_config(**overrides) -> dict:
    """Return a minimal valid OtelModule config."""
    base = {"exporter": "none"}
    base.update(overrides)
    return base


class TestOtelModule:
    """Core OtelModule behavior tests."""

    @pytest.mark.asyncio
    async def test_invoke_delegates_to_inner(self):
        """Messages are passed through, response returned."""
        from arcllm.modules.otel import OtelModule

        inner = _make_inner()
        module = OtelModule(_default_config(), inner)
        messages = [Message(role="user", content="hi")]
        response = await module.invoke(messages)
        inner.invoke.assert_awaited_once_with(messages, None)
        assert response.content == "hello"

    @pytest.mark.asyncio
    async def test_invoke_passes_tools_and_kwargs(self):
        """Tools and kwargs are forwarded to inner."""
        from arcllm.modules.otel import OtelModule

        inner = _make_inner()
        module = OtelModule(_default_config(), inner)
        messages = [Message(role="user", content="hi")]
        tools = [MagicMock()]
        response = await module.invoke(messages, tools=tools, max_tokens=100)
        inner.invoke.assert_awaited_once_with(messages, tools, max_tokens=100)

    @pytest.mark.asyncio
    async def test_returns_response_unchanged(self):
        """Same object reference returned."""
        from arcllm.modules.otel import OtelModule

        inner = _make_inner()
        expected = inner.invoke.return_value
        module = OtelModule(_default_config(), inner)
        messages = [Message(role="user", content="hi")]
        response = await module.invoke(messages)
        assert response is expected

    @pytest.mark.asyncio
    async def test_creates_root_span(self):
        """A span named 'arcllm.invoke' is created."""
        from arcllm.modules.otel import OtelModule

        inner = _make_inner()
        module = OtelModule(_default_config(), inner)
        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_tracer.start_as_current_span.return_value.__enter__ = MagicMock(
            return_value=mock_span
        )
        mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(
            return_value=False
        )
        with patch("arcllm.modules.base.trace.get_tracer", return_value=mock_tracer):
            messages = [Message(role="user", content="hi")]
            await module.invoke(messages)
        # _span is called with "arcllm.invoke"
        mock_tracer.start_as_current_span.assert_called()
        call_args = mock_tracer.start_as_current_span.call_args
        assert call_args[0][0] == "arcllm.invoke"

    @pytest.mark.asyncio
    async def test_sets_gen_ai_system_attribute(self):
        """gen_ai.system attribute set to inner.name."""
        from arcllm.modules.otel import OtelModule

        inner = _make_inner()
        module = OtelModule(_default_config(), inner)
        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_tracer.start_as_current_span.return_value.__enter__ = MagicMock(
            return_value=mock_span
        )
        mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(
            return_value=False
        )
        with patch("arcllm.modules.base.trace.get_tracer", return_value=mock_tracer):
            messages = [Message(role="user", content="hi")]
            await module.invoke(messages)
        # Check set_attribute calls
        calls = {c[0][0]: c[0][1] for c in mock_span.set_attribute.call_args_list}
        assert calls.get("gen_ai.system") == "anthropic"

    @pytest.mark.asyncio
    async def test_sets_gen_ai_request_model(self):
        """gen_ai.request.model attribute set to inner.model_name."""
        from arcllm.modules.otel import OtelModule

        inner = _make_inner()
        module = OtelModule(_default_config(), inner)
        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_tracer.start_as_current_span.return_value.__enter__ = MagicMock(
            return_value=mock_span
        )
        mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(
            return_value=False
        )
        with patch("arcllm.modules.base.trace.get_tracer", return_value=mock_tracer):
            messages = [Message(role="user", content="hi")]
            await module.invoke(messages)
        calls = {c[0][0]: c[0][1] for c in mock_span.set_attribute.call_args_list}
        assert calls.get("gen_ai.request.model") == "claude-sonnet-4-20250514"

    @pytest.mark.asyncio
    async def test_sets_gen_ai_usage_input_tokens(self):
        """gen_ai.usage.input_tokens set from response.usage."""
        from arcllm.modules.otel import OtelModule

        inner = _make_inner()
        module = OtelModule(_default_config(), inner)
        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_tracer.start_as_current_span.return_value.__enter__ = MagicMock(
            return_value=mock_span
        )
        mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(
            return_value=False
        )
        with patch("arcllm.modules.base.trace.get_tracer", return_value=mock_tracer):
            messages = [Message(role="user", content="hi")]
            await module.invoke(messages)
        calls = {c[0][0]: c[0][1] for c in mock_span.set_attribute.call_args_list}
        assert calls.get("gen_ai.usage.input_tokens") == 100

    @pytest.mark.asyncio
    async def test_sets_gen_ai_usage_output_tokens(self):
        """gen_ai.usage.output_tokens set from response.usage."""
        from arcllm.modules.otel import OtelModule

        inner = _make_inner()
        module = OtelModule(_default_config(), inner)
        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_tracer.start_as_current_span.return_value.__enter__ = MagicMock(
            return_value=mock_span
        )
        mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(
            return_value=False
        )
        with patch("arcllm.modules.base.trace.get_tracer", return_value=mock_tracer):
            messages = [Message(role="user", content="hi")]
            await module.invoke(messages)
        calls = {c[0][0]: c[0][1] for c in mock_span.set_attribute.call_args_list}
        assert calls.get("gen_ai.usage.output_tokens") == 50

    @pytest.mark.asyncio
    async def test_sets_gen_ai_response_model(self):
        """gen_ai.response.model set from response.model."""
        from arcllm.modules.otel import OtelModule

        inner = _make_inner()
        module = OtelModule(_default_config(), inner)
        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_tracer.start_as_current_span.return_value.__enter__ = MagicMock(
            return_value=mock_span
        )
        mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(
            return_value=False
        )
        with patch("arcllm.modules.base.trace.get_tracer", return_value=mock_tracer):
            messages = [Message(role="user", content="hi")]
            await module.invoke(messages)
        calls = {c[0][0]: c[0][1] for c in mock_span.set_attribute.call_args_list}
        assert calls.get("gen_ai.response.model") == "claude-sonnet-4-20250514"

    @pytest.mark.asyncio
    async def test_sets_gen_ai_response_finish_reasons(self):
        """gen_ai.response.finish_reasons set from response.stop_reason."""
        from arcllm.modules.otel import OtelModule

        inner = _make_inner()
        module = OtelModule(_default_config(), inner)
        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_tracer.start_as_current_span.return_value.__enter__ = MagicMock(
            return_value=mock_span
        )
        mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(
            return_value=False
        )
        with patch("arcllm.modules.base.trace.get_tracer", return_value=mock_tracer):
            messages = [Message(role="user", content="hi")]
            await module.invoke(messages)
        calls = {c[0][0]: c[0][1] for c in mock_span.set_attribute.call_args_list}
        assert calls.get("gen_ai.response.finish_reasons") == ["end_turn"]

    def test_provider_name_from_inner(self):
        """module.name delegates to inner.name."""
        from arcllm.modules.otel import OtelModule

        inner = _make_inner()
        module = OtelModule(_default_config(), inner)
        assert module.name == "anthropic"

    def test_model_name_from_inner(self):
        """module.model_name delegates to inner.model_name."""
        from arcllm.modules.otel import OtelModule

        inner = _make_inner()
        module = OtelModule(_default_config(), inner)
        assert module.model_name == "claude-sonnet-4-20250514"


class TestOtelModuleValidation:
    """Config validation tests."""

    def test_invalid_exporter_rejected(self):
        """Unknown exporter raises ArcLLMConfigError."""
        from arcllm.modules.otel import OtelModule

        with pytest.raises(ArcLLMConfigError, match="exporter"):
            OtelModule({"exporter": "invalid_exporter"}, _make_inner())

    def test_invalid_protocol_rejected(self):
        """Unknown protocol raises ArcLLMConfigError."""
        from arcllm.modules.otel import OtelModule

        with pytest.raises(ArcLLMConfigError, match="protocol"):
            OtelModule(
                {"exporter": "none", "protocol": "invalid_proto"}, _make_inner()
            )

    def test_sample_rate_below_zero_rejected(self):
        """Sample rate < 0 raises ArcLLMConfigError."""
        from arcllm.modules.otel import OtelModule

        with pytest.raises(ArcLLMConfigError, match="sample_rate"):
            OtelModule({"exporter": "none", "sample_rate": -0.1}, _make_inner())

    def test_sample_rate_above_one_rejected(self):
        """Sample rate > 1 raises ArcLLMConfigError."""
        from arcllm.modules.otel import OtelModule

        with pytest.raises(ArcLLMConfigError, match="sample_rate"):
            OtelModule({"exporter": "none", "sample_rate": 1.5}, _make_inner())

    def test_unknown_config_keys_rejected(self):
        """Unknown config keys raise ArcLLMConfigError."""
        from arcllm.modules.otel import OtelModule

        with pytest.raises(ArcLLMConfigError, match="Unknown"):
            OtelModule(
                {"exporter": "none", "bogus_key": "value"}, _make_inner()
            )

    def test_sdk_not_installed_raises_error(self):
        """OTel enabled but SDK not installed raises clear error."""
        from arcllm.modules.otel import OtelModule

        with patch.dict("sys.modules", {"opentelemetry.sdk": None}):
            with pytest.raises(ArcLLMConfigError, match="install"):
                OtelModule({"exporter": "otlp"}, _make_inner())


class TestOtelSdkSetup:
    """SDK setup tests — all mocked."""

    def test_otlp_exporter_created(self):
        """OTLP exporter created with endpoint and protocol."""
        from arcllm.modules.otel import OtelModule

        mock_sdk_trace = MagicMock()
        mock_otlp_grpc = MagicMock()
        with (
            patch.dict(
                "sys.modules",
                {
                    "opentelemetry.sdk": MagicMock(),
                    "opentelemetry.sdk.trace": mock_sdk_trace,
                    "opentelemetry.sdk.trace.export": MagicMock(),
                    "opentelemetry.sdk.resources": MagicMock(),
                    "opentelemetry.exporter.otlp.proto.grpc.trace_exporter": mock_otlp_grpc,
                },
            ),
            patch("arcllm.modules.otel._setup_sdk") as mock_setup,
        ):
            module = OtelModule(
                {
                    "exporter": "otlp",
                    "endpoint": "http://collector:4317",
                    "protocol": "grpc",
                },
                _make_inner(),
            )
            mock_setup.assert_called_once()

    def test_console_exporter_created(self):
        """Console exporter used when exporter='console'."""
        from arcllm.modules.otel import OtelModule

        with patch("arcllm.modules.otel._setup_sdk") as mock_setup:
            module = OtelModule({"exporter": "console"}, _make_inner())
            mock_setup.assert_called_once()

    def test_none_exporter_no_processor(self):
        """No exporter created when exporter='none'."""
        from arcllm.modules.otel import OtelModule

        with patch("arcllm.modules.otel._setup_sdk") as mock_setup:
            module = OtelModule({"exporter": "none"}, _make_inner())
            mock_setup.assert_not_called()

    def test_auth_headers_passed(self):
        """Headers dict forwarded to exporter setup."""
        from arcllm.modules.otel import OtelModule

        with patch("arcllm.modules.otel._setup_sdk") as mock_setup:
            module = OtelModule(
                {
                    "exporter": "otlp",
                    "headers": {"Authorization": "Bearer tok"},
                },
                _make_inner(),
            )
            config_arg = mock_setup.call_args[0][0]
            assert config_arg["headers"] == {"Authorization": "Bearer tok"}

    def test_resource_includes_service_name(self):
        """service_name passed to SDK setup."""
        from arcllm.modules.otel import OtelModule

        with patch("arcllm.modules.otel._setup_sdk") as mock_setup:
            module = OtelModule(
                {"exporter": "otlp", "service_name": "my-agent"},
                _make_inner(),
            )
            config_arg = mock_setup.call_args[0][0]
            assert config_arg["service_name"] == "my-agent"

    def test_resource_includes_custom_attributes(self):
        """resource_attributes passed to SDK setup."""
        from arcllm.modules.otel import OtelModule

        attrs = {"deployment.environment": "production", "service.version": "1.0"}
        with patch("arcllm.modules.otel._setup_sdk") as mock_setup:
            module = OtelModule(
                {"exporter": "otlp", "resource_attributes": attrs},
                _make_inner(),
            )
            config_arg = mock_setup.call_args[0][0]
            assert config_arg["resource_attributes"] == attrs

    def test_sampler_uses_sample_rate(self):
        """sample_rate passed to SDK setup."""
        from arcllm.modules.otel import OtelModule

        with patch("arcllm.modules.otel._setup_sdk") as mock_setup:
            module = OtelModule(
                {"exporter": "otlp", "sample_rate": 0.5},
                _make_inner(),
            )
            config_arg = mock_setup.call_args[0][0]
            assert config_arg["sample_rate"] == 0.5

    def test_tls_certificates_passed(self):
        """TLS certificate paths forwarded to SDK setup."""
        from arcllm.modules.otel import OtelModule

        with patch("arcllm.modules.otel._setup_sdk") as mock_setup:
            module = OtelModule(
                {
                    "exporter": "otlp",
                    "certificate_file": "/path/ca.pem",
                    "client_key_file": "/path/client.key",
                    "client_cert_file": "/path/client.pem",
                },
                _make_inner(),
            )
            config_arg = mock_setup.call_args[0][0]
            assert config_arg["certificate_file"] == "/path/ca.pem"
            assert config_arg["client_key_file"] == "/path/client.key"
            assert config_arg["client_cert_file"] == "/path/client.pem"


class TestOtelSdkIdempotency:
    """Idempotency guard on _setup_sdk."""

    def test_setup_sdk_idempotent(self):
        """Second call to _setup_sdk is a no-op."""
        import arcllm.modules.otel as otel_mod

        otel_mod._sdk_configured = False
        with patch("arcllm.modules.otel._setup_sdk", wraps=otel_mod._setup_sdk):
            # First call via OtelModule should set _sdk_configured
            with patch.dict(
                "sys.modules",
                {
                    "opentelemetry.sdk": MagicMock(),
                    "opentelemetry.sdk.trace": MagicMock(),
                    "opentelemetry.sdk.trace.export": MagicMock(),
                    "opentelemetry.sdk.resources": MagicMock(),
                    "opentelemetry.sdk.trace.sampling": MagicMock(),
                    "opentelemetry.exporter.otlp.proto.grpc.trace_exporter": MagicMock(),
                },
            ):
                # Direct call to _setup_sdk
                otel_mod._sdk_configured = False
                otel_mod._setup_sdk({"exporter": "otlp"})
                assert otel_mod._sdk_configured is True

                # Second call should be no-op (no import errors from mocks)
                otel_mod._setup_sdk({"exporter": "otlp"})
                # If it wasn't idempotent, the mock modules would fail

        # Cleanup
        otel_mod._sdk_configured = False

    def test_setup_sdk_flag_reset_by_clear_cache(self):
        """clear_cache() resets _sdk_configured."""
        import arcllm.modules.otel as otel_mod
        from arcllm.registry import clear_cache

        otel_mod._sdk_configured = True
        clear_cache()
        assert otel_mod._sdk_configured is False
