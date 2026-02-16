"""OtelModule — OpenTelemetry distributed tracing root span with GenAI attributes."""

import logging
from typing import Any

from arcllm.exceptions import ArcLLMConfigError
from arcllm.modules.base import BaseModule
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
    except ImportError:
        raise ArcLLMConfigError(
            "OTel SDK not installed. Run: pip install arcllm[otel]"
        )

    from opentelemetry import trace

    # Resource
    resource_attrs = {"service.name": config.get("service_name", "arcllm")}
    resource_attrs.update(config.get("resource_attributes", {}))
    resource = Resource.create(resource_attrs)

    # Sampler
    sample_rate = config.get("sample_rate", 1.0)
    sampler = TraceIdRatioBased(sample_rate)

    # TracerProvider
    provider = TracerProvider(resource=resource, sampler=sampler)

    # Exporter
    exporter_type = config.get("exporter", "otlp")
    certificate_file = config.get("certificate_file")
    client_key_file = config.get("client_key_file")
    client_cert_file = config.get("client_cert_file")

    if exporter_type == "otlp":
        endpoint = config.get("endpoint", "http://localhost:4317")
        protocol = config.get("protocol", "grpc")
        headers = config.get("headers", {})
        insecure = config.get("insecure", False)
        timeout_ms = config.get("timeout_ms", 10000)

        if protocol == "grpc":
            try:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                    OTLPSpanExporter,
                )
            except ImportError:
                raise ArcLLMConfigError(
                    "OTLP gRPC exporter not installed. "
                    "Run: pip install arcllm[otel]"
                )
            # Read TLS certificate files for gRPC credentials
            credentials_kwargs: dict[str, Any] = {}
            if certificate_file:
                credentials_kwargs["certificate_file"] = certificate_file
            if client_key_file:
                credentials_kwargs["client_key_file"] = client_key_file
            if client_cert_file:
                credentials_kwargs["client_certificate_file"] = client_cert_file
            exporter = OTLPSpanExporter(
                endpoint=endpoint,
                headers=headers or None,
                insecure=insecure,
                timeout=timeout_ms // 1000,
                **credentials_kwargs,
            )
        else:  # http
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter,
                )
            except ImportError:
                raise ArcLLMConfigError(
                    "OTLP HTTP exporter not installed. "
                    "Run: pip install arcllm[otel]"
                )
            # HTTP exporter uses certificate_file for TLS CA verification
            http_kwargs: dict[str, Any] = {}
            if certificate_file:
                http_kwargs["certificate_file"] = certificate_file
            if client_key_file:
                http_kwargs["client_key_file"] = client_key_file
            if client_cert_file:
                http_kwargs["client_certificate_file"] = client_cert_file
            exporter = OTLPSpanExporter(
                endpoint=endpoint,
                headers=headers or None,
                timeout=timeout_ms // 1000,
                **http_kwargs,
            )
    elif exporter_type == "console":
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter

        exporter = ConsoleSpanExporter()
    else:
        return  # Should not reach here due to validation

    # Processor with batch tuning
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

        # Validate config keys
        unknown = set(config.keys()) - _VALID_CONFIG_KEYS
        if unknown:
            raise ArcLLMConfigError(
                f"Unknown OtelModule config keys: {sorted(unknown)}. "
                f"Valid keys: {sorted(_VALID_CONFIG_KEYS - {'enabled'})}"
            )

        # Validate exporter
        exporter = config.get("exporter", "otlp")
        if exporter not in _VALID_EXPORTERS:
            raise ArcLLMConfigError(
                f"Invalid exporter '{exporter}'. "
                f"Valid exporters: {sorted(_VALID_EXPORTERS)}"
            )

        # Validate protocol
        protocol = config.get("protocol", "grpc")
        if protocol not in _VALID_PROTOCOLS:
            raise ArcLLMConfigError(
                f"Invalid protocol '{protocol}'. "
                f"Valid protocols: {sorted(_VALID_PROTOCOLS)}"
            )

        # Validate sample_rate
        sample_rate = config.get("sample_rate", 1.0)
        if sample_rate < 0.0:
            raise ArcLLMConfigError("sample_rate must be >= 0.0")
        if sample_rate > 1.0:
            raise ArcLLMConfigError("sample_rate must be <= 1.0")

        # Setup SDK if exporter is not 'none'
        if exporter != "none":
            _setup_sdk(config)

    async def invoke(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        with self._span("arcllm.invoke") as span:
            # Pre-call attributes
            span.set_attribute("gen_ai.system", self._inner.name)
            span.set_attribute("gen_ai.request.model", self._inner.model_name)

            response = await self._inner.invoke(messages, tools, **kwargs)

            # Post-call attributes from response
            span.set_attribute(
                "gen_ai.usage.input_tokens", response.usage.input_tokens
            )
            span.set_attribute(
                "gen_ai.usage.output_tokens", response.usage.output_tokens
            )
            span.set_attribute("gen_ai.response.model", response.model)
            span.set_attribute(
                "gen_ai.response.finish_reasons", [response.stop_reason]
            )

            return response
