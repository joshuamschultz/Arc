"""ArcLLM modules — opt-in functionality that wraps adapters."""

from arcllm.modules.audit import AuditModule
from arcllm.modules.base import BaseModule
from arcllm.modules.fallback import FallbackModule
from arcllm.modules.otel import OtelModule
from arcllm.modules.rate_limit import RateLimitModule
from arcllm.modules.retry import RetryModule
from arcllm.modules.security import SecurityModule
from arcllm.modules.telemetry import TelemetryModule

__all__ = [
    "AuditModule",
    "BaseModule",
    "FallbackModule",
    "OtelModule",
    "RateLimitModule",
    "RetryModule",
    "SecurityModule",
    "TelemetryModule",
]
