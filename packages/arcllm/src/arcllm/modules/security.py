"""SecurityModule — PII redaction and request signing middleware."""

from __future__ import annotations

import json
from typing import Any

from arcllm._pii import PiiDetector, RegexPiiDetector, redact_text
from arcllm._signing import canonical_payload, create_signer, RequestSigner
from arcllm.exceptions import ArcLLMConfigError
from arcllm.modules.base import BaseModule
from arcllm.types import (
    ContentBlock,
    LLMProvider,
    LLMResponse,
    Message,
    TextBlock,
    Tool,
    ToolResultBlock,
    ToolUseBlock,
)

_VALID_CONFIG_KEYS = {
    "pii_enabled",
    "pii_detector",
    "pii_custom_patterns",
    "signing_enabled",
    "signing_algorithm",
    "signing_key_env",
    "enabled",
}

_VALID_DETECTORS = {"regex"}


class SecurityModule(BaseModule):
    """Per-invoke security middleware: PII redaction + request signing.

    Phases per invoke():
        1. Redact PII from outbound messages (to LLM)
        2. Call inner.invoke() with redacted messages
        3. Redact PII from inbound response (from LLM)
        4. Sign request payload and attach to response metadata

    Stack position: Audit -> Security -> Retry
    (Audit sees redacted data; each retry sends redacted+signed request)
    """

    def __init__(self, config: dict[str, Any], inner: LLMProvider) -> None:
        super().__init__(config, inner)

        unknown = set(config.keys()) - _VALID_CONFIG_KEYS
        if unknown:
            raise ArcLLMConfigError(
                f"Unknown SecurityModule config keys: {sorted(unknown)}. "
                f"Valid keys: {sorted(_VALID_CONFIG_KEYS - {'enabled'})}"
            )

        self._pii_enabled: bool = config.get("pii_enabled", True)
        self._signing_enabled: bool = config.get("signing_enabled", True)

        # Build PII detector (lazy — only if PII enabled)
        self._pii_detector: PiiDetector | None = None
        if self._pii_enabled:
            detector_type = config.get("pii_detector", "regex")
            custom_patterns = config.get("pii_custom_patterns", [])
            if detector_type not in _VALID_DETECTORS:
                raise ArcLLMConfigError(
                    f"Unsupported pii_detector type: {detector_type!r}. "
                    f"Supported: {sorted(_VALID_DETECTORS)}"
                )
            self._pii_detector = RegexPiiDetector(
                custom_patterns=custom_patterns or None
            )

        # Build signer (lazy — only if signing enabled)
        self._signer: RequestSigner | None = None
        self._signing_algorithm: str = config.get("signing_algorithm", "hmac-sha256")
        if self._signing_enabled:
            signing_key_env = config.get("signing_key_env", "ARCLLM_SIGNING_KEY")
            self._signer = create_signer(self._signing_algorithm, signing_key_env)

    async def invoke(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        with self._span("security"):
            # Phase 1: PII redaction on outbound messages
            if self._pii_enabled and self._pii_detector is not None:
                with self._span("security.pii_redact_outbound"):
                    messages = self._redact_messages(messages)

            # Phase 2: Call inner provider
            response = await self._inner.invoke(messages, tools, **kwargs)

            # Phase 3: PII redaction on inbound response
            if self._pii_enabled and self._pii_detector is not None:
                with self._span("security.pii_redact_inbound"):
                    response = self._redact_response(response)

            # Phase 4: Sign request and attach to response
            if self._signing_enabled and self._signer is not None:
                with self._span("security.sign"):
                    payload = canonical_payload(messages, tools, self.model_name)
                    signature = self._signer.sign(payload)
                    response = self._attach_signature(response, signature)

            return response

    def _redact_messages(self, messages: list[Message]) -> list[Message]:
        """Redact PII from all messages, returning new list."""
        result: list[Message] = []
        for msg in messages:
            if isinstance(msg.content, str):
                redacted_content = self._redact_str(msg.content)
                result.append(Message(role=msg.role, content=redacted_content))
            elif isinstance(msg.content, list):
                redacted_blocks = self._redact_blocks(msg.content)
                result.append(Message(role=msg.role, content=redacted_blocks))
            else:
                result.append(msg)
        return result

    def _redact_blocks(self, blocks: list[ContentBlock]) -> list[ContentBlock]:
        """Redact PII from ContentBlock list."""
        result: list[ContentBlock] = []
        for block in blocks:
            if isinstance(block, TextBlock):
                redacted = self._redact_str(block.text)
                result.append(TextBlock(text=redacted))
            elif isinstance(block, ToolResultBlock):
                if isinstance(block.content, str):
                    redacted = self._redact_str(block.content)
                    result.append(
                        ToolResultBlock(
                            tool_use_id=block.tool_use_id,
                            content=redacted,
                        )
                    )
                else:
                    result.append(block)
            elif isinstance(block, ToolUseBlock):
                # Scan arguments as JSON string
                args_str = json.dumps(block.arguments)
                redacted_str = self._redact_str(args_str)
                if redacted_str != args_str:
                    redacted_args = json.loads(redacted_str)
                    result.append(
                        ToolUseBlock(
                            id=block.id,
                            name=block.name,
                            arguments=redacted_args,
                        )
                    )
                else:
                    result.append(block)
            else:
                # ImageBlock and others pass through
                result.append(block)
        return result

    def _redact_str(self, text: str) -> str:
        """Detect and redact PII in a string."""
        matches = self._pii_detector.detect(text)
        if not matches:
            return text
        return redact_text(text, matches)

    def _redact_response(self, response: LLMResponse) -> LLMResponse:
        """Redact PII from response content."""
        if response.content is None:
            return response

        if isinstance(response.content, str):
            redacted = self._redact_str(response.content)
            if redacted != response.content:
                return response.model_copy(update={"content": redacted})

        return response

    def _attach_signature(
        self, response: LLMResponse, signature: str
    ) -> LLMResponse:
        """Attach signing metadata to response."""
        metadata = dict(response.metadata) if response.metadata else {}
        metadata["request_signature"] = signature
        metadata["signing_algorithm"] = self._signing_algorithm

        return response.model_copy(update={"metadata": metadata})
