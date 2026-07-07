"""SecurityModule — PII redaction and request signing middleware."""

from __future__ import annotations

import importlib
import json
from typing import Any

from arctrust.fips import assert_fips_if_required
from arctrust.signer import Signer

from arcllm._pii import PiiDetector, RegexPiiDetector, redact_text
from arcllm._signing import canonical_payload, create_signer
from arcllm.exceptions import ArcLLMConfigError
from arcllm.modules.base import BaseModule, validate_config_keys
from arcllm.types import (
    ContentBlock,
    LLMProvider,
    LLMResponse,
    Message,
    TextBlock,
    Tool,
    ToolCall,
    ToolResultBlock,
    ToolUseBlock,
)

_VALID_CONFIG_KEYS = {
    "pii_enabled",
    "pii_detector",
    "pii_custom_patterns",
    "pii_entities",
    "pii_detector_class",
    "signing_enabled",
    "signing_algorithm",
    "signing_key_env",
    "require_fips",
    "enabled",
}

# Allowlisted pii_detector_class module prefixes (ASI04 supply-chain guard).
# Mirrors vault.py's VaultResolver.from_config exactly (D-427): this
# narrows the *namespace* importlib will import from — it does NOT sandbox
# or verify the loaded code's trustworthiness. Signature verification of
# the loaded package/class is arctrust/arcagent's job, not arcllm's.
_ALLOWED_DETECTOR_PREFIXES = ("arcllm.", "arcagent.", "arcpii.")


def _load_detector_class(ref: str) -> PiiDetector:
    """Load a custom PiiDetector via an allowlisted ``module:Class`` reference.

    Order is load-bearing: the prefix allowlist gate runs BEFORE
    ``import_module``, because import executes the target module's
    top-level code (import-time RCE) — checking after import would be
    too late (ASI04/ASI05).

    Args:
        ref: ``"module.path:ClassName"`` format. The loaded class must
            accept a no-argument constructor — custom detectors manage
            their own configuration, not ``pii_custom_patterns``/
            ``pii_entities``.

    Raises:
        ArcLLMConfigError: On invalid format, non-allowlisted module,
            missing module/class, or a class that fails the PiiDetector
            protocol check.
    """
    if ":" not in ref:
        raise ArcLLMConfigError(f"pii_detector_class must be 'module:Class' format, got: '{ref}'")

    module_path, class_name = ref.rsplit(":", 1)

    if not any(module_path.startswith(prefix) for prefix in _ALLOWED_DETECTOR_PREFIXES):
        raise ArcLLMConfigError(
            f"pii_detector_class module '{module_path}' is not in the allowlist. "
            f"Allowed prefixes: {list(_ALLOWED_DETECTOR_PREFIXES)}"
        )

    try:
        module = importlib.import_module(module_path)
    except ImportError as e:
        raise ArcLLMConfigError(
            f"pii_detector_class '{ref}' not installed. Could not import module '{module_path}'."
        ) from e

    detector_class = getattr(module, class_name, None)
    if detector_class is None:
        raise ArcLLMConfigError(f"pii_detector_class '{class_name}' not found in '{module_path}'")

    instance = detector_class()
    if not isinstance(instance, PiiDetector):
        raise ArcLLMConfigError(
            f"pii_detector_class '{ref}' does not implement PiiDetector.detect()"
        )
    return instance


class SecurityModule(BaseModule):
    """Per-invoke security middleware: PII redaction + request signing.

    Phases per invoke():
        1. Redact PII from outbound messages (to LLM)
        2. Call inner.invoke() with redacted messages
        3. Redact PII from inbound response (from LLM)
        4. Sign request payload and attach to response metadata

    Stack position: Audit -> Guardrails -> Injection -> Security -> CircuitBreaker
    (Audit sees redacted data; each retry sends redacted+signed request)

    PII detector selection (D-093/FR-13, ADR-427):
        - ``pii_detector_class`` set -> allowlisted ``module:Class`` loader
          (wins over ``pii_detector`` when both are set).
        - otherwise -> built-in ``RegexPiiDetector``, enriched with
          checksum-gated entities, gov/CUI categories, and the SECRETS
          category (Spec 015). Any ``pii_detector`` value other than
          selecting ``pii_detector_class`` simply resolves to the
          built-in detector.
    """

    def __init__(self, config: dict[str, Any], inner: LLMProvider) -> None:
        super().__init__(config, inner)
        validate_config_keys(config, _VALID_CONFIG_KEYS, "SecurityModule")

        # Build PII detector (lazy — only if PII enabled)
        self._pii_detector: PiiDetector | None = None
        if config.get("pii_enabled", True):
            custom_patterns = config.get("pii_custom_patterns", [])
            entities = config.get("pii_entities") or None
            detector_class_ref = config.get("pii_detector_class", "")
            if detector_class_ref:
                self._pii_detector = _load_detector_class(detector_class_ref)
            else:
                self._pii_detector = RegexPiiDetector(
                    custom_patterns=custom_patterns or None,
                    entities=entities,
                )

        # Build signer (lazy — only if signing enabled). Asymmetric by default
        # (Ed25519); ecdsa-p256 for the FIPS/federal path. HMAC is gone.
        self._signer: Signer | None = None
        self._signing_algorithm: str = config.get("signing_algorithm", "ed25519")
        if config.get("signing_enabled", True):
            # Same generalized arctrust FIPS gate as trace encryption: at federal
            # (require_fips=true) request signing must use a FIPS-validated
            # backend AND a FIPS-approved algorithm — fail closed before any
            # attestation, so arcllm never claims FIPS while signing with
            # non-validated Ed25519 (SPEC-037 secondary).
            assert_fips_if_required(
                require_fips=config.get("require_fips", False),
                algorithm=self._signing_algorithm,
            )
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
            if self._pii_detector is not None:
                with self._span("security.pii_redact_outbound"):
                    messages = self._redact_messages(messages)

            # Phase 2: Call inner provider
            response = await self._inner.invoke(messages, tools, **kwargs)

            # Phase 3: PII redaction on inbound response
            if self._pii_detector is not None:
                with self._span("security.pii_redact_inbound"):
                    response = self._redact_response(response)

            # Phase 4: Sign request and attach to response. The model label
            # bound into the signed payload is the config-resolved model_name
            # (never an attacker-suppliable response field) — REQ-011.
            if self._signer is not None:
                with self._span("security.sign"):
                    payload = canonical_payload(messages, tools, self.model_name)
                    signature = self._signer.sign(payload).hex()
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
                result.append(
                    ToolResultBlock(
                        tool_use_id=block.tool_use_id,
                        content=self._redact_tool_result_content(block.content),
                    )
                )
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

    def _redact_tool_result_content(
        self, content: str | list[ContentBlock]
    ) -> str | list[ContentBlock]:
        """Redact a ToolResultBlock's content, recursing into nested TextBlocks.

        A structured tool result (``list[ContentBlock]``) is exactly the
        vector PII redaction protects against (ASI06) — it must not be a
        blind spot just because it's a list instead of a plain string.
        """
        if isinstance(content, str):
            return self._redact_str(content)
        return self._redact_blocks(content)

    def _redact_str(self, text: str) -> str:
        """Detect and redact PII in a string.

        Callers must ensure _pii_detector is not None before calling.
        Assert enforces this invariant structurally.
        """
        if self._pii_detector is None:  # pragma: no cover — structural guard
            return text
        matches = self._pii_detector.detect(text)
        if not matches:
            return text
        return redact_text(text, matches)

    def _redact_response(self, response: LLMResponse) -> LLMResponse:
        """Redact PII from response content and tool-call arguments (M5).

        Tool-call arguments are just as capable of carrying PII/secrets as
        ``response.content`` — mirrors the outbound ``ToolUseBlock.arguments``
        handling in ``_redact_blocks``.
        """
        updates: dict[str, Any] = {}

        if isinstance(response.content, str):
            redacted_content = self._redact_str(response.content)
            if redacted_content != response.content:
                updates["content"] = redacted_content

        if response.tool_calls:
            redacted_calls = self._redact_tool_calls(response.tool_calls)
            if redacted_calls != response.tool_calls:
                updates["tool_calls"] = redacted_calls

        if not updates:
            return response
        return response.model_copy(update=updates)

    def _redact_tool_calls(self, tool_calls: list[ToolCall]) -> list[ToolCall]:
        """Redact PII/secrets from each tool call's JSON-serialized arguments."""
        result: list[ToolCall] = []
        for call in tool_calls:
            args_str = json.dumps(call.arguments)
            redacted_str = self._redact_str(args_str)
            if redacted_str != args_str:
                result.append(
                    ToolCall(id=call.id, name=call.name, arguments=json.loads(redacted_str))
                )
            else:
                result.append(call)
        return result

    def _attach_signature(self, response: LLMResponse, signature: str) -> LLMResponse:
        """Attach asymmetric signing metadata to the response.

        The public key rides alongside so a downstream verifier can check the
        attestation with public material only (AU-10 non-repudiation) — it never
        needs, and never receives, signing material.
        """
        metadata = dict(response.metadata) if response.metadata else {}
        metadata["request_signature"] = signature
        metadata["signing_algorithm"] = self._signing_algorithm
        if self._signer is not None:
            metadata["signing_public_key"] = self._signer.public_key.hex()

        return response.model_copy(update={"metadata": metadata})
