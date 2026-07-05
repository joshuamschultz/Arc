"""ArcLLM exception hierarchy."""

from __future__ import annotations

from typing import TYPE_CHECKING

# Type-only imports — keeps exceptions.py at the bottom of the dependency
# graph (no runtime import of modules/injection.py or modules/guardrails.py,
# which would create a cycle since those modules import from here).
if TYPE_CHECKING:
    from arcllm.modules.guardrails import Violation
    from arcllm.modules.injection import InjectionFinding


class ArcLLMError(Exception):
    """Base exception for all ArcLLM errors."""


class ArcLLMParseError(ArcLLMError):
    """Raised when tool call arguments cannot be parsed.

    Stores the raw string and original error so agents can log,
    retry, or surface the failure.
    """

    def __init__(self, raw_string: str, original_error: Exception) -> None:
        self.raw_string = raw_string
        self.original_error = original_error
        super().__init__(f"Failed to parse tool call arguments: {original_error}")


class ArcLLMConfigError(ArcLLMError):
    """Raised on configuration validation failure."""


class ArcLLMBudgetError(ArcLLMError):
    """Raised when a budget limit would be exceeded.

    Carries scope, limit type, and dollar amounts so callers can decide
    whether to retry later, switch to a cheaper model, or alert.
    """

    def __init__(
        self,
        scope: str,
        limit_type: str,
        limit_usd: float,
        current_usd: float,
        estimated_usd: float | None,
    ) -> None:
        self.scope = scope
        self.limit_type = limit_type
        self.limit_usd = limit_usd
        self.current_usd = current_usd
        self.estimated_usd = estimated_usd
        super().__init__(
            f"Budget {limit_type} limit exceeded for '{scope}': "
            f"limit=${limit_usd:.2f}, current=${current_usd:.2f}"
        )


_MAX_ERROR_BODY_DISPLAY = 500


class ArcLLMAPIError(ArcLLMError):
    """Raised when a provider API returns an HTTP error.

    Carries status_code, body, and provider so agents and the retry
    module can make smart decisions (e.g., 429 → retry, 401 → don't).
    The full body is on the attribute; __str__ truncates to prevent
    leaking verbose provider error details into logs.
    """

    def __init__(
        self,
        status_code: int,
        body: str,
        provider: str,
        retry_after: float | None = None,
    ) -> None:
        self.status_code = status_code
        self.body = body
        self.provider = provider
        self.retry_after = retry_after
        display_body = (
            body[:_MAX_ERROR_BODY_DISPLAY] + "..." if len(body) > _MAX_ERROR_BODY_DISPLAY else body
        )
        super().__init__(f"{provider} API error (HTTP {status_code}): {display_body}")


class QueueFullError(ArcLLMError):
    """Raised when queue backpressure rejects a call.

    The queue has reached ``max_queued`` waiting callers.  The caller
    should decide whether to drop, retry later, or alert.
    """

    def __init__(self, current_waiters: int, max_queued: int) -> None:
        self.current_waiters = current_waiters
        self.max_queued = max_queued
        super().__init__(f"Queue full: {current_waiters} calls waiting (max {max_queued})")


class QueueTimeoutError(ArcLLMError):
    """Raised when a call exceeds the send-time timeout.

    The timeout starts *after* the semaphore is acquired — it measures
    actual provider response time, not queue wait.
    """

    def __init__(self, timeout: float) -> None:
        self.timeout = timeout
        super().__init__(f"LLM call timed out after {timeout:.1f}s (send-time)")


class ArcLLMInjectionError(ArcLLMError):
    """Raised when InjectionModule detects a prompt-injection pattern in block mode.

    Carries the structured findings so callers can branch (log, alert,
    human-gate) without string-matching the exception message (D-431).
    """

    def __init__(self, findings: list[InjectionFinding]) -> None:
        self.findings = findings
        super().__init__(f"Prompt injection detected: {len(findings)} finding(s)")


class ArcLLMGuardrailError(ArcLLMError):
    """Raised when GuardrailsModule finds a structural violation in block mode.

    Carries the structured violations so callers can branch (retry with a
    stricter prompt, alert, human-gate) without string-matching (D-431).
    """

    def __init__(self, violations: list[Violation]) -> None:
        self.violations = violations
        super().__init__(f"Output guardrail violation: {len(violations)} rule(s)")


class ArcLLMTraceNotFoundError(ArcLLMError):
    """Raised by ``load_for_replay`` when no record matches the given trace_id."""

    def __init__(self, trace_id: str) -> None:
        self.trace_id = trace_id
        super().__init__(f"Trace '{trace_id}' not found for replay")


class ArcLLMTraceIntegrityError(ArcLLMError):
    """Raised when an encrypted trace envelope fails tamper-evidence checks.

    Distinct from ``ArcLLMConfigError`` (a misconfiguration) — this signals
    a detected integrity violation (AAD/record-identity mismatch), e.g. a
    ciphertext transplanted onto a different record (D-448, AU-10).
    """
