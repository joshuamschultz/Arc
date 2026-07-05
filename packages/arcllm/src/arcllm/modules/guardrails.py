"""GuardrailsModule — structural output validation (OWASP LLM05).

Opt-in per call. Validates the **final** resolved response (after
Retry/Fallback/Security/Injection have all resolved) for JSON-schema
conformance, regex allow/deny, max-length, and a banned-content
stop-list. Structural only — semantic judgement (grounding, factual
correctness, toxicity) is explicitly out of scope (ADR-429) and lives in
arcagent/arcrun.

A PASSED guardrail is not the same as output that is safe to render at
a downstream sink (HTML/shell/DB). Structural validation catches shape
violations; the caller must still context-encode for its sink (SDD
Research Insight #2).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from arcllm._scan_limits import MAX_REGEX_SCAN_LENGTH as _MAX_SCAN_LENGTH
from arcllm.exceptions import ArcLLMConfigError, ArcLLMGuardrailError
from arcllm.modules.base import BaseModule, resolve_enforcement, validate_config_keys
from arcllm.types import LLMProvider, LLMResponse, Message, Tool

_VALID_CONFIG_KEYS = {
    "enforcement",
    "json_schema",
    "allow_patterns",
    "deny_patterns",
    "max_length",
    "banned_content",
    "enabled",
}


@dataclass(frozen=True)
class Violation:
    """One structural guardrail violation."""

    rule: str
    detail: str


def _extract_text(content: str | None) -> str:
    """Normalize response content to a string for text-based checks.

    ``LLMResponse.content`` is ``str | None`` (unlike ``Message.content``,
    which can carry a ``list[ContentBlock]``) — response content is
    always plain text or absent, so no block-concatenation is needed.
    """
    return content or ""


class GuardrailsModule(BaseModule):
    """Validates the resolved response's STRUCTURE only (ADR-429).

    Config keys:
        enforcement: "block" (raise ArcLLMGuardrailError) or "warn" (flag
            into ``response.metadata["guardrail_violations"]``).
        json_schema: JSON Schema dict. Response content is parsed as
            JSON and validated against it. Requires
            ``arcllm[guardrails-schema]`` — gated behind the extra so the
            verdict never silently depends on whether the optional
            dependency happens to be installed.
        allow_patterns / deny_patterns: Regex lists, compiled once.
        max_length: Character cap on response content (0 = uncapped).
        banned_content: Case-insensitive phrase stop-list.
    """

    def __init__(self, config: dict[str, Any], inner: LLMProvider) -> None:
        super().__init__(config, inner)
        validate_config_keys(config, _VALID_CONFIG_KEYS, "GuardrailsModule")

        self._enforcement: str = resolve_enforcement(config)

        self._json_schema: dict[str, Any] | None = config.get("json_schema")
        self._max_length: int = config.get("max_length", 0)
        self._banned_content: list[str] = list(config.get("banned_content", []))
        self._allow_patterns = self._compile_patterns(
            config.get("allow_patterns", []), "allow_patterns"
        )
        self._deny_patterns = self._compile_patterns(
            config.get("deny_patterns", []), "deny_patterns"
        )

        if self._json_schema is not None:
            self._require_jsonschema_extra()

    @staticmethod
    def _compile_patterns(patterns: list[str], key: str) -> list[re.Pattern[str]]:
        compiled: list[re.Pattern[str]] = []
        for p in patterns:
            try:
                compiled.append(re.compile(p))
            except re.error as e:
                raise ArcLLMConfigError(f"Invalid regex in {key}: {e}") from e
        return compiled

    @staticmethod
    def _require_jsonschema_extra() -> None:
        """Fail loudly at construction if jsonschema is unavailable.

        Gated behind ``arcllm[guardrails-schema]`` so the verdict never
        silently depends on whether the optional dependency is installed
        (SDD Research Insight #3 — deterministic enforcement, mirrors the
        injection-semantic gating pattern).
        """
        try:
            import jsonschema  # noqa: F401
        except ImportError as e:
            raise ArcLLMConfigError(
                "json_schema guardrail requires arcllm[guardrails-schema] "
                "(pip install arcllm[guardrails-schema])"
            ) from e

    async def invoke(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        with self._span("arcllm.guardrails") as span:
            response = await self._inner.invoke(messages, tools, **kwargs)
            violations = self._validate(response)
            span.set_attribute("arcllm.guardrails.violations", len(violations))
            span.set_attribute("arcllm.guardrails.enforcement", self._enforcement)
            if violations:
                if self._enforcement == "block":
                    raise ArcLLMGuardrailError(violations)
                return self._flag(response, violations)
            return response

    def _validate(self, response: LLMResponse) -> list[Violation]:
        text = _extract_text(response.content)
        # Only the operator-supplied deny/allow REGEX scans run against a
        # length-capped prefix (ReDoS guard) — those are the only checks
        # whose cost can blow up on a crafted pattern. json_schema,
        # max_length, and banned_content (a plain substring scan, no
        # backtracking risk) all need the FULL text: capping them would
        # falsely pass an over-length response, corrupt JSON mid-token, or
        # silently miss a banned phrase placed past the cap.
        scan_text = text[:_MAX_SCAN_LENGTH]
        violations: list[Violation] = []

        if self._json_schema is not None:
            violations.extend(self._check_schema(text))
        if self._deny_patterns:
            violations.extend(self._check_deny(scan_text))
        if self._allow_patterns:
            violations.extend(self._check_allow(scan_text))
        if self._max_length:
            violations.extend(self._check_max_length(text))
        if self._banned_content:
            violations.extend(self._check_banned(text))
        return violations

    def _check_schema(self, text: str) -> list[Violation]:
        import jsonschema

        try:
            parsed = json.loads(text) if text else None
        except json.JSONDecodeError:
            return [Violation(rule="json_schema", detail="response content is not valid JSON")]
        try:
            jsonschema.validate(instance=parsed, schema=self._json_schema)
        except jsonschema.ValidationError as e:
            return [Violation(rule="json_schema", detail=str(e.message))]
        return []

    def _check_deny(self, text: str) -> list[Violation]:
        for pattern in self._deny_patterns:
            if pattern.search(text):
                detail = f"matched deny pattern: {pattern.pattern}"
                return [Violation(rule="deny_pattern", detail=detail)]
        return []

    def _check_allow(self, text: str) -> list[Violation]:
        for pattern in self._allow_patterns:
            if pattern.search(text):
                return []
        return [Violation(rule="allow_pattern", detail="content did not match any allow pattern")]

    def _check_max_length(self, text: str) -> list[Violation]:
        if len(text) > self._max_length:
            return [
                Violation(
                    rule="max_length",
                    detail=f"content length {len(text)} exceeds max {self._max_length}",
                )
            ]
        return []

    def _check_banned(self, text: str) -> list[Violation]:
        lowered = text.lower()
        for phrase in self._banned_content:
            if phrase.lower() in lowered:
                detail = f"banned phrase detected: {phrase}"
                return [Violation(rule="banned_content", detail=detail)]
        return []

    def _flag(self, response: LLMResponse, violations: list[Violation]) -> LLMResponse:
        """Attach violations to response.metadata and return (warn mode)."""
        metadata = dict(response.metadata) if response.metadata else {}
        metadata["guardrail_violations"] = [
            {"rule": v.rule, "detail": v.detail} for v in violations
        ]
        return response.model_copy(update={"metadata": metadata})
