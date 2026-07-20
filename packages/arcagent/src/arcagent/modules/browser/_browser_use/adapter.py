"""Route browser-use's LLM calls through arcllm.

browser-use drives its agent by calling a ``BaseChatModel``
(``browser_use.llm.base``) — a runtime-checkable Protocol needing
``model``/``provider``/``name`` and one ``ainvoke`` method. This adapter
implements that surface over an arcllm ``LLMProvider`` so every LLM call
browser-use makes goes through arcllm's PII redaction, audit, and
provider config — no rogue LLM path.

browser-use types (``ChatInvokeCompletion``, message classes) are
imported lazily inside ``ainvoke``; the arcllm-facing conversion is kept
import-safe so it is unit-testable without the optional extra installed.
Structured output uses a single-tool call (provider-agnostic across the
arcllm anthropic/openai adapters) rather than ``response_format``, which
anthropic rejects.
"""

from __future__ import annotations

from typing import Any

from arcllm.types import ImageBlock, Message, TextBlock, Tool

_OUTPUT_TOOL = "emit_output"


def _arc_content(content: Any) -> str | list[Any]:
    """Convert a browser-use message ``content`` to arcllm content.

    ``content`` is a plain string or a list of typed parts (text /
    image_url). Parts are duck-typed by their ``type`` field so this needs
    no browser_use import.
    """
    if isinstance(content, str):
        return content
    if not content:
        return ""
    blocks: list[Any] = []
    for part in content:
        kind = getattr(part, "type", None)
        if kind == "text":
            blocks.append(TextBlock(text=part.text))
        elif kind == "image_url":
            image = part.image_url
            blocks.append(ImageBlock(source=image.url, media_type=image.media_type))
    return blocks or ""


def _to_arc_messages(bu_messages: list[Any]) -> list[Message]:
    """Map browser-use messages to arcllm messages (role + content)."""
    return [Message(role=m.role, content=_arc_content(m.content)) for m in bu_messages]


class ArcLLMChatModel:
    """A browser-use ``BaseChatModel`` backed by an arcllm provider."""

    def __init__(self, provider: Any, *, model: str) -> None:
        self._provider = provider
        self.model = model
        self._last_response: Any = None

    @property
    def provider(self) -> str:
        return "arcllm"

    @property
    def name(self) -> str:
        return self.model

    async def ainvoke(
        self, messages: list[Any], output_format: Any = None, **_kwargs: Any
    ) -> Any:
        """Call arcllm and wrap the result as a ``ChatInvokeCompletion``."""
        from browser_use.llm.views import ChatInvokeCompletion, ChatInvokeUsage

        arc_messages = _to_arc_messages(messages)

        if output_format is None:
            resp = await self._provider.invoke(arc_messages)
            completion: Any = resp.content or ""
        else:
            completion = await self._invoke_structured(arc_messages, output_format)
            resp = self._last_response

        usage = None
        if resp.usage is not None:
            u = resp.usage
            usage = ChatInvokeUsage(
                prompt_tokens=u.input_tokens,
                prompt_cached_tokens=u.cache_read_tokens,
                prompt_cache_creation_tokens=u.cache_write_tokens,
                prompt_image_tokens=None,
                completion_tokens=u.output_tokens,
                total_tokens=u.total_tokens,
            )
        return ChatInvokeCompletion(
            completion=completion, usage=usage, stop_reason=resp.stop_reason
        )

    async def _invoke_structured(self, arc_messages: list[Message], output_format: Any) -> Any:
        """Force a structured result and validate it into ``output_format``.

        Uses a single ``emit_output`` tool whose parameters are the target
        schema — works across arcllm's anthropic (tool_use) and openai
        adapters. Falls back to parsing the message content as JSON when a
        model answers in prose instead of calling the tool.
        """
        tool = Tool(
            name=_OUTPUT_TOOL,
            description="Return the structured result by calling this tool.",
            parameters=output_format.model_json_schema(),
        )
        resp = await self._provider.invoke(arc_messages, tools=[tool])
        self._last_response = resp
        for call in resp.tool_calls:
            if call.name == _OUTPUT_TOOL:
                return output_format.model_validate(call.arguments)
        if resp.content:
            return output_format.model_validate_json(resp.content)
        raise ValueError("browser-use LLM adapter: no structured output returned")
