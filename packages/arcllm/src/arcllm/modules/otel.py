"""OtelModule — OpenTelemetry distributed tracing root span with GenAI attributes."""

import logging
from typing import Any

from arcllm.exceptions import ArcLLMConfigError
from arcllm.modules.base import BaseModule, validate_config_keys
from arcllm.types import LLMProvider, LLMResponse, Message, Tool

logger = logging.getLogger(__name__)

_VALID_EXPORTERS = {"otlp", "console", "none"}
_VALID_PROTOCOLS = {"grpc", "http"}
_VALID_CONFIG_KEYS = {
    "enabled",
    "exporter",
    "endpoint",
    "protocol",
    "service_name",
    "sample_rate",
    "headers",
    "insecure",
    "certificate_file",
    "client_key_file",
    "client_cert_file",
    "timeout_ms",
    "max_batch_size",
    "max_queue_size",
    "schedule_delay_ms",
    "resource_attributes",
}


_sdk_configured = False


def reset_sdk() -> None:
    """Reset the SDK configured flag (for test isolation)."""
    global _sdk_configured
    _sdk_configured = False


def _build_tls_kwargs(config: dict[str, Any]) -> dict[str, Any]:
    """Build TLS credential kwargs from config (shared by gRPC and HTTP)."""
    kwargs: dict[str, Any] = {}
    if config.get("certificate_file"):
        kwargs["certificate_file"] = config["certificate_file"]
    if config.get("client_key_file"):
        kwargs["client_key_file"] = config["client_key_file"]
    if config.get("client_cert_file"):
        kwargs["client_certificate_file"] = config["client_cert_file"]
    return kwargs


def _create_otlp_exporter(config: dict[str, Any]) -> Any:
    """Create an OTLP exporter (gRPC or HTTP) from config."""
    protocol = config.get("protocol", "grpc")
    endpoint = config.get("endpoint", "http://localhost:4317")
    headers = config.get("headers", {})
    insecure = config.get("insecure", False)
    timeout_ms = config.get("timeout_ms", 10000)
    tls_kwargs = _build_tls_kwargs(config)

    if protocol == "grpc":
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )
        except ImportError as e:
            raise ArcLLMConfigError(
                "OTLP gRPC exporter not installed. Run: pip install arcllm[otel]"
            ) from e
        return OTLPSpanExporter(
            endpoint=endpoint,
            headers=headers or None,
            insecure=insecure,
            timeout=timeout_ms // 1000,
            **tls_kwargs,
        )

    # HTTP protocol
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
    except ImportError as e:
        raise ArcLLMConfigError(
            "OTLP HTTP exporter not installed. Run: pip install arcllm[otel]"
        ) from e
    return OTLPSpanExporter(
        endpoint=endpoint,
        headers=headers or None,
        timeout=timeout_ms // 1000,
        **tls_kwargs,
    )


def _create_exporter(config: dict[str, Any]) -> Any:
    """Create the appropriate span exporter from config."""
    exporter_type = config.get("exporter", "otlp")

    if exporter_type == "otlp":
        return _create_otlp_exporter(config)

    if exporter_type == "console":
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter

        return ConsoleSpanExporter()

    return None  # "none" — validated upstream, should not reach here


def _setup_sdk(config: dict[str, Any]) -> None:
    """Configure OTel SDK TracerProvider, exporter, sampler, and processor.

    Requires opentelemetry-sdk to be installed. Called only when
    exporter != 'none'. Idempotent — skips setup if already configured.
    """
    global _sdk_configured
    if _sdk_configured:
        return

    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import TraceIdRatioBased
    except ImportError as e:
        raise ArcLLMConfigError("OTel SDK not installed. Run: pip install arcllm[otel]") from e

    from opentelemetry import trace

    # Resource
    resource_attrs = {"service.name": config.get("service_name", "arcllm")}
    resource_attrs.update(config.get("resource_attributes", {}))
    resource = Resource.create(resource_attrs)

    # Sampler + Provider
    sampler = TraceIdRatioBased(config.get("sample_rate", 1.0))
    provider = TracerProvider(resource=resource, sampler=sampler)

    # Exporter + Processor
    exporter = _create_exporter(config)
    if exporter is None:
        return

    processor = BatchSpanProcessor(
        exporter,
        max_queue_size=config.get("max_queue_size", 2048),
        max_export_batch_size=config.get("max_batch_size", 512),
        schedule_delay_millis=config.get("schedule_delay_ms", 5000),
    )
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)
    _sdk_configured = True


class OtelModule(BaseModule):
    """Creates root 'arcllm.invoke' span with GenAI semantic convention attributes.

    Sits outermost in the module stack. Auto-nests under parent span when
    agent framework provides one via OTel context propagation.

    Config keys:
        exporter: 'otlp', 'console', or 'none' (default: 'otlp').
        endpoint: OTLP collector endpoint (default: 'http://localhost:4317').
        protocol: 'grpc' or 'http' (default: 'grpc').
        service_name: OTel service.name resource attribute (default: 'arcllm').
        sample_rate: Trace sampling rate 0.0-1.0 (default: 1.0).
        headers: Dict of auth headers for OTLP exporter.
        insecure: Allow insecure gRPC connections (default: False).
        certificate_file: TLS CA certificate path.
        client_key_file: mTLS client key path.
        client_cert_file: mTLS client certificate path.
        timeout_ms: Export timeout in milliseconds (default: 10000).
        max_batch_size: BatchSpanProcessor max export batch (default: 512).
        max_queue_size: BatchSpanProcessor max queue (default: 2048).
        schedule_delay_ms: BatchSpanProcessor schedule delay (default: 5000).
        resource_attributes: Additional OTel Resource attributes.
    """

    def __init__(self, config: dict[str, Any], inner: LLMProvider) -> None:
        super().__init__(config, inner)
        validate_config_keys(config, _VALID_CONFIG_KEYS, "OtelModule")

        exporter = config.get("exporter", "otlp")
        if exporter not in _VALID_EXPORTERS:
            raise ArcLLMConfigError(
                f"Invalid exporter '{exporter}'. Valid exporters: {sorted(_VALID_EXPORTERS)}"
            )

        protocol = config.get("protocol", "grpc")
        if protocol not in _VALID_PROTOCOLS:
            raise ArcLLMConfigError(
                f"Invalid protocol '{protocol}'. Valid protocols: {sorted(_VALID_PROTOCOLS)}"
            )

        sample_rate = config.get("sample_rate", 1.0)
        if not 0.0 <= sample_rate <= 1.0:
            raise ArcLLMConfigError("sample_rate must be between 0.0 and 1.0")

        if exporter != "none":
            _setup_sdk(config)

    async def invoke(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        with self._span("arcllm.invoke") as span:
            span.set_attribute("gen_ai.system", self._inner.name)
            span.set_attribute("gen_ai.request.model", self._inner.model_name)

            response = await self._inner.invoke(messages, tools, **kwargs)

            span.set_attribute("gen_ai.usage.input_tokens", response.usage.input_tokens)
            span.set_attribute("gen_ai.usage.output_tokens", response.usage.output_tokens)
            span.set_attribute("gen_ai.response.model", response.model)
            span.set_attribute("gen_ai.response.finish_reasons", [response.stop_reason])

            return response
