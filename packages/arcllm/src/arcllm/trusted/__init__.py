"""Arc-owned trusted middleware — wraps any backend implementing the contract.

This layer applies Arc's duties uniformly regardless of backend:
- Telemetry: arcstore spool writes, JSONL trace store, FIPS gate
- Audit: arctrust WORM audit events
- Security: PII redaction (arctrust), request signing (arctrust)
- Injection: prompt injection detection
- Guardrails: output validation
- Queue: durable queue journal
- Otel: OpenTelemetry instrumentation
- Budget: cost tracking and limits

The middleware wraps ANY backend that implements arcllm.contract.ModelProvider,
so no backend can skip telemetry or audit.

RELOCATION SCENARIOS (if wrapper moves later):

1. Stay in arcllm (current):
   - No moves required
   - arcllm.trusted stays here
   - Consumers import from arcllm

2. Move to arcrun:
   - Move: arcllm/trusted/ → arcrun/model_wrapper/
   - Update: arcllm/loader.py wrapper import
   - Benefits: wrapper closer to primary consumer
   - Contract and backends stay in arcllm

3. Move to new package (e.g., arcwrap, arctelem):
   - Move: arcllm/trusted/ → newpkg/
   - Update: arcllm/loader.py wrapper import
   - Benefits: separate trust/telemetry layer
   - Contract and backends stay in arcllm
   - New package depends on: arcllm.contract, arcstore, arctrust

All three options keep contract and backends in arcllm. Only the wrapper moves.
Consumers continue calling arcllm.load_model() — only the factory's internal
import changes.
"""

from arcllm.trusted.wrapper import TrustedWrapperBase

# To be populated in subsequent commits with middleware from arcllm.modules:
# from arcllm.trusted.telemetry import TelemetryMiddleware
# from arcllm.trusted.security import SecurityMiddleware
# from arcllm.trusted.injection import InjectionMiddleware
# from arcllm.trusted.guardrails import GuardrailsMiddleware
# from arcllm.trusted.queue import QueueMiddleware
# from arcllm.trusted.otel import OtelMiddleware

__all__ = [
    "TrustedWrapperBase",
    # Will be populated as middleware moves from arcllm.modules
]
