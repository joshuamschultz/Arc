"""SecurityModule — PII redaction and request signing middleware."""

from __future__ import annotations

import importlib
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

from arctrust.fips import assert_fips_if_required
from arctrust.signer import Signer

from arcllm._pii import PiiDetector, RegexPiiDetector, redact_text
from arcllm._signing import canonical_payload, create_signer
from arcllm.exceptions import ArcLLMConfigError
from arcllm.modules.base import BaseModule, validate_config_keys
from arcllm.types import (
    ContentBlock,
    Delta,
    LLMProvider,
    LLMResponse,
    Message,
    TextBlock,
    Tool,
    ToolCall,
    ToolCallDelta,
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
_STREAM_REDACTION_TAIL = 128


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


def _build_detector(config: dict[str, Any]) -> PiiDetector | None:
    """Build the configured PII detector, or None when redaction is disabled."""
    if not config.get("pii_enabled", True):
        return None
    detector_class_ref = config.get("pii_detector_class", "")
    if detector_class_ref:
        return _load_detector_class(detector_class_ref)
    return RegexPiiDetector(
        custom_patterns=config.get("pii_custom_patterns") or None,
        entities=config.get("pii_entities") or None,
    )


def configured_redactor() -> Callable[[str], str]:
    """Return the deployment's PII redaction, resolved from its configured settings.

    The one entry point for a caller that builds a payload OUTSIDE an LLM
    round-trip — a workflow activation, a skill mutation — and therefore never
    passes through :class:`SecurityModule`. Such a payload would otherwise carry
    no policy at all, while everything the model touched carried the tier's.

    Returns identity when the deployment has not enabled redaction, so the
    configured policy decides and the caller never overrides it.
    """
    from arcllm.registry import _resolve_module_config

    config = _resolve_module_config("security", None)
    detector = _build_detector(config) if config is not None else None
    if detector is None:
        return lambda text: text

    def redact(text: str) -> str:
        matches = detector.detect(text)
        return redact_text(text, matches) if matches else text

    return redact


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
        self._pii_detector: PiiDetector | None = _build_detector(config)

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
                    response = self._attach_signature(
                        response, self._signature_metadata(messages, tools)
                    )

            return response

    async def invoke_stream(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[Delta]:
        """Stream through the same redaction and signing boundary as ``invoke``.

        Tool argument fragments are held until the provider closes its stream so a
        secret split across arbitrary wire chunks cannot escape redaction and the
        replacement remains one valid JSON fragment for ``StreamAccumulator``.
        """
        with self._span("security"):
            if self._pii_detector is not None:
                with self._span("security.pii_redact_outbound"):
                    messages = self._redact_messages(messages)

            signature: dict[str, Any] = {}
            if self._signer is not None:
                with self._span("security.sign"):
                    signature = self._signature_metadata(messages, tools)

            stream = self._inner.invoke_stream(messages, tools, **kwargs)
            argument_fragments: dict[int, list[str]] = {}
            text_buffer = [""]
            terminal: Delta | None = None
            try:
                async for delta in stream:
                    safe = self._redact_delta(delta, argument_fragments)
                    if self._pii_detector is not None and delta.text:
                        text_buffer[0] += delta.text
                        safe = safe.model_copy(
                            update={"text": self._flush_text_prefix(text_buffer, final=False)}
                        )
                    if safe.stop_reason is not None:
                        terminal = safe
                    elif not _empty_delta(safe):
                        yield safe

                if text_buffer[0]:
                    yield Delta(text=self._redact_str(text_buffer[0]))
                for index, fragments in argument_fragments.items():
                    arguments = "".join(fragments)
                    if self._pii_detector is not None:
                        arguments = self._redact_str(arguments)
                    yield Delta(tool_call=ToolCallDelta(index=index, arguments=arguments))

                if terminal is not None:
                    if signature:
                        terminal = terminal.model_copy(
                            update={"metadata": {**(terminal.metadata or {}), **signature}}
                        )
                    yield terminal
                elif signature:
                    yield Delta(metadata=signature)
            finally:
                close = getattr(stream, "aclose", None)
                if close is not None:
                    await close()

    def _redact_delta(self, delta: Delta, argument_fragments: dict[int, list[str]]) -> Delta:
        """Redact visible text and hold tool argument fragments for final assembly."""
        updates: dict[str, Any] = {}
        tool_call = delta.tool_call
        if tool_call is not None:
            if tool_call.arguments is not None:
                argument_fragments.setdefault(tool_call.index, []).append(tool_call.arguments)
                tool_call = tool_call.model_copy(update={"arguments": None})
            if self._pii_detector is not None:
                tool_call = tool_call.model_copy(
                    update={
                        "id": self._redact_str(tool_call.id) if tool_call.id else None,
                        "name": self._redact_str(tool_call.name) if tool_call.name else None,
                    }
                )
            updates["tool_call"] = tool_call
        return delta.model_copy(update=updates) if updates else delta

    def _flush_text_prefix(self, buffer: list[str], *, final: bool) -> str | None:
        """Flush only text proven not to contain a PII match split across chunks."""
        if final:
            prefix, buffer[0] = buffer[0], ""
        else:
            cut = max(0, len(buffer[0]) - _STREAM_REDACTION_TAIL)
            if cut == 0:
                return None
            matches = self._pii_detector.detect(buffer[0]) if self._pii_detector else []
            cut = min(
                (match.start for match in matches if match.start < cut < match.end),
                default=cut,
            )
            prefix, buffer[0] = buffer[0][:cut], buffer[0][cut:]
        return self._redact_str(prefix) if prefix else None

    def _signature_metadata(
        self, messages: list[Message], tools: list[Tool] | None
    ) -> dict[str, Any]:
        """Build the same public request attestation metadata used by ``invoke``."""
        if self._signer is None:
            return {}
        payload = canonical_payload(messages, tools, self.model_name)
        return {
            "request_signature": self._signer.sign(payload).hex(),
            "signing_algorithm": self._signing_algorithm,
            "signing_public_key": self._signer.public_key.hex(),
        }

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

    def _attach_signature(self, response: LLMResponse, signature: dict[str, Any]) -> LLMResponse:
        """Attach asymmetric signing metadata to the response.

        The public key rides alongside so a downstream verifier can check the
        attestation with public material only (AU-10 non-repudiation) — it never
        needs, and never receives, signing material.
        """
        metadata = dict(response.metadata) if response.metadata else {}
        metadata.update(signature)

        return response.model_copy(update={"metadata": metadata})


def _empty_delta(delta: Delta) -> bool:
    """Return whether a redaction pass left no stream-visible fields."""
    return not any((delta.text, delta.tool_call, delta.usage, delta.metadata))
