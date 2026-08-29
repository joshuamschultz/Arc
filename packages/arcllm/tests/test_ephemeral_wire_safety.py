"""H-038 security confirm #1: an ``ephemeral`` message is sent, its flag never is.

``ephemeral`` (arcllm.types.Message) means "skip for routing / persistence" —
never "skip for sending". The model MUST still see the words (arcrun's
per-call current-time block is useless to the model otherwise). What must
never happen is the *marker itself* — the literal field name — leaking into a
provider's wire payload, which would happen if an adapter naive-serialized
the whole Pydantic model (``.model_dump()``) instead of hand-picking fields.

Two adapters are covered because they are the only two independent
``_format_message`` implementations in the tree: ``AnthropicAdapter`` (its
own) and ``OpenaiAdapter`` (inherited by every OpenAI-compatible provider —
azure_openai, cohere, deepseek, fireworks, groq, huggingface,
huggingface_tgi, litellm, mistral, moonshot, ollama, together, vllm, xai — 13
adapters share this one code path, confirmed by ``grep '^class'`` on
``arcllm/adapters/*.py``). Neither ``_format_message`` calls
``model_dump()``/``.dict()`` anywhere in ``arcllm/adapters/`` (confirmed by
grep) — both build the wire dict field by field, which is what actually keeps
``ephemeral`` off the wire; these tests pin that behavior so a future
refactor toward ``model_dump()`` fails loudly instead of silently leaking.
"""

from __future__ import annotations

import json

import pytest

from arcllm.adapters.anthropic import AnthropicAdapter
from arcllm.adapters.openai import OpenaiAdapter
from arcllm.config import ModelMetadata, ProviderConfig, ProviderSettings
from arcllm.types import Message

_TIME_TEXT = "Current date/time: 2026-08-29 14:32 UTC"


def _messages() -> list[Message]:
    return [
        Message(role="system", content="You are helpful."),
        Message(role="user", content="Do the real task."),
        Message(role="user", content=_TIME_TEXT, ephemeral=True),
    ]


def _no_leak_and_sent(body: dict) -> None:
    blob = json.dumps(body)
    assert _TIME_TEXT in blob, "the ephemeral message's words must reach the wire"
    assert "ephemeral" not in blob, "the ephemeral MARKER must never reach the wire"


@pytest.fixture(autouse=True)
def _set_test_api_keys(monkeypatch):
    monkeypatch.setenv("ARCLLM_TEST_ANTHROPIC_KEY", "test-ant-key")
    monkeypatch.setenv("ARCLLM_TEST_OPENAI_KEY", "test-openai-key")


class TestAnthropicEphemeralWireSafety:
    def test_ephemeral_text_sent_flag_omitted(self):
        settings = ProviderSettings(
            api_format="anthropic-messages",
            base_url="https://api.anthropic.com",
            api_key_env="ARCLLM_TEST_ANTHROPIC_KEY",
            default_model="claude-test-1",
            default_temperature=0.7,
            # Caching-off: plain-wire shape, so the equality assertion below
            # is not chasing Anthropic's own (unrelated) cache_control block —
            # that mechanism is covered by SPEC-029's own test suite.
            enable_prompt_caching=False,
        )
        meta = ModelMetadata(
            context_window=200000,
            max_output_tokens=8192,
            supports_tools=True,
            supports_vision=True,
            supports_thinking=True,
            input_modalities=["text", "image"],
            cost_input_per_1m=3.0,
            cost_output_per_1m=15.0,
            cost_cache_read_per_1m=0.3,
            cost_cache_write_per_1m=3.75,
        )
        config = ProviderConfig(provider=settings, models={"claude-test-1": meta})
        adapter = AnthropicAdapter(config, "claude-test-1")

        body = adapter._build_request_body(_messages())

        # The ephemeral message really is in the wire messages array, as an
        # ordinary trailing user-role entry — not folded into cached `system`.
        assert body["messages"][-1] == {"role": "user", "content": _TIME_TEXT}
        _no_leak_and_sent(body)


class TestOpenAIEphemeralWireSafety:
    def test_ephemeral_text_sent_flag_omitted(self):
        settings = ProviderSettings(
            api_format="openai-chat",
            base_url="https://api.openai.com",
            api_key_env="ARCLLM_TEST_OPENAI_KEY",
            default_model="gpt-4o-test",
            default_temperature=0.7,
        )
        meta = ModelMetadata(
            context_window=128000,
            max_output_tokens=16384,
            supports_tools=True,
            supports_vision=True,
            supports_thinking=False,
            input_modalities=["text", "image"],
            cost_input_per_1m=2.5,
            cost_output_per_1m=10.0,
            cost_cache_read_per_1m=1.25,
            cost_cache_write_per_1m=2.5,
        )
        config = ProviderConfig(provider=settings, models={"gpt-4o-test": meta})
        adapter = OpenaiAdapter(config, "gpt-4o-test")

        body = adapter._build_request_body(_messages())

        assert body["messages"][-1] == {"role": "user", "content": _TIME_TEXT}
        _no_leak_and_sent(body)
