"""AuditModule — structured audit logging of LLM interactions per invoke()."""

import logging
from typing import Any

from arcllm.exceptions import ArcLLMConfigError
from arcllm.modules._logging import _sanitize, log_structured, validate_log_level
from arcllm.modules.base import BaseModule
from arcllm.types import LLMProvider, LLMResponse, Message, Tool

logger = logging.getLogger(__name__)

_VALID_CONFIG_KEYS = {
    "include_messages",
    "include_response",
    "log_level",
    "enabled",
}


class AuditModule(BaseModule):
    """Wraps invoke() to log audit metadata for compliance and debugging.

    PII-safe by default: only metadata is logged (provider, model, message
    count, stop reason, content length, tool counts).  Raw message/response
    content is opt-in via config flags and logged at DEBUG level.

    Config keys:
        include_messages: Log raw message content at DEBUG (default: False).
        include_response: Log raw response content at DEBUG (default: False).
        log_level: Python log level name (default: "INFO").
    """

    def __init__(self, config: dict[str, Any], inner: LLMProvider) -> None:
        super().__init__(config, inner)

        unknown = set(config.keys()) - _VALID_CONFIG_KEYS
        if unknown:
            raise ArcLLMConfigError(
                f"Unknown AuditModule config keys: {sorted(unknown)}. "
                f"Valid keys: {sorted(_VALID_CONFIG_KEYS - {'enabled'})}"
            )

        self._include_messages: bool = config.get("include_messages", False)
        self._include_response: bool = config.get("include_response", False)
        self._log_level: int = validate_log_level(config)

    async def invoke(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        with self._span("arcllm.audit") as audit_span:
            response = await self._inner.invoke(messages, tools, **kwargs)

            content_length = len(response.content) if response.content else 0

            audit_span.set_attribute("arcllm.audit.message_count", len(messages))
            audit_span.set_attribute("arcllm.audit.content_length", content_length)
            if tools is not None:
                audit_span.set_attribute("arcllm.audit.tools_provided", len(tools))
            if response.tool_calls:
                audit_span.set_attribute(
                    "arcllm.audit.tool_calls", len(response.tool_calls)
                )

            log_structured(
                logger,
                self._log_level,
                "Audit",
                provider=self._inner.name,
                model=response.model,
                message_count=len(messages),
                stop_reason=response.stop_reason,
                tools_provided=len(tools) if tools is not None else None,
                tool_calls=len(response.tool_calls) if response.tool_calls else None,
                content_length=content_length,
            )

            if self._include_messages and logger.isEnabledFor(logging.DEBUG):
                logger.debug("Audit messages | %s", _sanitize(str(messages)))
            if self._include_response and logger.isEnabledFor(logging.DEBUG):
                logger.debug("Audit response | %s", _sanitize(str(response.content)))

            return response
