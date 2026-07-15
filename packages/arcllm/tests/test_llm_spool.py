"""arcllm → arcstore.spool auto-instrumentation (SPEC-026 FR-4).

Every completion records one ``llm_call`` spool line by default — on success
AND on error (finally-guarded, closes defect C3). Recording is gated by
``arcstore_enabled`` (default true) and imports only ``arcstore.spool`` —
never the store/backends (module boundary, AC-4.3).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import arcllm.modules.telemetry as telemetry_mod
from arcllm.modules.telemetry import TelemetryModule
from arcllm.types import LLMProvider, LLMResponse, Message, Usage

_OK = LLMResponse(
    content="ok",
    usage=Usage(input_tokens=100, output_tokens=50, total_tokens=150),
    model="test-model",
    stop_reason="end_turn",
)


def _inner() -> MagicMock:
    inner = MagicMock(spec=LLMProvider)
    inner.name = "test-provider"
    inner.model_name = "test-model"
    inner.invoke = AsyncMock(return_value=_OK)
    return inner


def _config(**overrides: object) -> dict:
    base = {
        "cost_input_per_1m": 3.0,
        "cost_output_per_1m": 15.0,
        "agent_did": "did:arc:acme:analyst/aabbccdd",
    }
    base.update(overrides)
    return base


@pytest.fixture
def messages() -> list[Message]:
    return [Message(role="user", content="hi")]


async def test_completion_records_llm_call(messages: list[Message]) -> None:
    module = TelemetryModule(_config(), _inner())
    recorded: list = []
    with patch.object(telemetry_mod, "_spool_record", recorded.append):
        await module.invoke(messages)
    assert len(recorded) == 1
    rec = recorded[0]
    assert rec.kind == "llm_call"
    assert rec.actor_did == "did:arc:acme:analyst/aabbccdd"
    assert rec.outcome == "ok"
    assert rec.model == "test-model"
    assert rec.prompt_tokens == 100
    assert rec.completion_tokens == 50
    assert rec.cost_usd is not None and rec.cost_usd > 0
    assert rec.latency_ms is not None


async def test_completion_records_cache_breakdown(messages: list[Message]) -> None:
    """Cache read/write tokens are persisted SEPARATELY on the spool record so a
    consumer can compute hit-rate — prompt_tokens stays the summed input total."""
    inner = _inner()
    inner.invoke = AsyncMock(
        return_value=LLMResponse(
            content="ok",
            usage=Usage(
                input_tokens=2,
                output_tokens=50,
                total_tokens=52,
                cache_read_tokens=1500,
                cache_write_tokens=300,
            ),
            model="test-model",
            stop_reason="end_turn",
        )
    )
    module = TelemetryModule(_config(), inner)
    recorded: list = []
    with patch.object(telemetry_mod, "_spool_record", recorded.append):
        await module.invoke(messages)
    rec = recorded[0]
    # prompt_tokens = total input context (input + cache_read + cache_write)
    assert rec.prompt_tokens == 2 + 1500 + 300
    # breakdown persisted separately
    assert rec.cache_read_tokens == 1500
    assert rec.cache_write_tokens == 300


async def test_completion_records_none_cache_when_absent(messages: list[Message]) -> None:
    """A provider that reports no cache usage leaves the breakdown fields None."""
    module = TelemetryModule(_config(), _inner())
    recorded: list = []
    with patch.object(telemetry_mod, "_spool_record", recorded.append):
        await module.invoke(messages)
    rec = recorded[0]
    assert rec.cache_read_tokens is None
    assert rec.cache_write_tokens is None


async def test_error_call_records_outcome_error(messages: list[Message]) -> None:
    inner = _inner()
    inner.invoke = AsyncMock(side_effect=RuntimeError("boom"))
    module = TelemetryModule(_config(), inner)
    recorded: list = []
    with patch.object(telemetry_mod, "_spool_record", recorded.append):
        with pytest.raises(RuntimeError):
            await module.invoke(messages)
    assert len(recorded) == 1
    assert recorded[0].outcome == "error"
    # The failing row must carry the KNOWN model (not None → dashed column) and a
    # diagnosable error reason, so a flapping instance isn't a bare `— / error`.
    assert recorded[0].model == inner.model_name
    assert recorded[0].model is not None
    assert "boom" in recorded[0].extra["error"]
    assert "RuntimeError" in recorded[0].extra["error"]


async def test_disabled_records_nothing(messages: list[Message]) -> None:
    module = TelemetryModule(_config(arcstore_enabled=False), _inner())
    recorded: list = []
    with patch.object(telemetry_mod, "_spool_record", recorded.append):
        await module.invoke(messages)
    assert recorded == []


async def test_raw_bodies_ride_extra_when_enabled(messages: list[Message]) -> None:
    """store_raw_bodies=True parks request/response payloads in spool extra so
    the UI can show the actual call, not just metadata."""
    module = TelemetryModule(_config(store_raw_bodies=True), _inner())
    recorded: list = []
    with patch.object(telemetry_mod, "_spool_record", recorded.append):
        await module.invoke(messages)
    extra = recorded[0].extra
    assert extra["request_body"]["messages"][0]["content"] == "hi"
    assert extra["response_body"]["content"] == "ok"


async def test_raw_bodies_in_extra_by_default(messages: list[Message]) -> None:
    """SPEC-016 D-435 — full capture is now the default: payloads DO appear in extra."""
    module = TelemetryModule(_config(), _inner())
    recorded: list = []
    with patch.object(telemetry_mod, "_spool_record", recorded.append):
        await module.invoke(messages)
    assert "request_body" in recorded[0].extra
    assert "response_body" in recorded[0].extra


async def test_no_raw_bodies_in_extra_when_disabled(messages: list[Message]) -> None:
    """store_raw_bodies=False (explicit, audited downgrade) omits payloads from extra."""
    module = TelemetryModule(_config(store_raw_bodies=False), _inner())
    recorded: list = []
    with patch.object(telemetry_mod, "_spool_record", recorded.append):
        await module.invoke(messages)
    assert "request_body" not in recorded[0].extra
    assert "response_body" not in recorded[0].extra


async def test_error_path_carries_request_body_only(messages: list[Message]) -> None:
    """An erroring call still records the request payload (no response exists)."""
    inner = _inner()
    inner.invoke = AsyncMock(side_effect=RuntimeError("boom"))
    module = TelemetryModule(_config(store_raw_bodies=True), inner)
    recorded: list = []
    with patch.object(telemetry_mod, "_spool_record", recorded.append):
        with pytest.raises(RuntimeError):
            await module.invoke(messages)
    extra = recorded[0].extra
    assert extra["request_body"]["messages"][0]["content"] == "hi"
    assert "response_body" not in extra


# ---------------------------------------------------------------------------
# H1 — encryption must never leak plaintext bodies into the spool, and
# bodies must be built exactly once per invoke() (folded-in M4).
# ---------------------------------------------------------------------------


def _wrapping_key_secret() -> str:
    import base64

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    return base64.b64encode(AESGCM.generate_key(bit_length=256)).decode("ascii")


async def test_spool_never_carries_plaintext_bodies_when_encryption_enabled(
    messages: list[Message],
) -> None:
    """H1: encryption enabled must seal bodies before EITHER consumer (the
    trace_store record or the arcstore spool) ever sees them — the spool
    must not be a plaintext-CUI side channel around encryption."""
    module = TelemetryModule(
        _config(
            store_raw_bodies=True,
            encryption={"enabled": True, "key_ref": "v1"},
            encryption_key_secret=_wrapping_key_secret(),
        ),
        _inner(),
    )
    recorded: list = []
    with patch.object(telemetry_mod, "_spool_record", recorded.append):
        await module.invoke(messages)

    extra = recorded[0].extra
    assert "request_body" not in extra
    assert "response_body" not in extra
    serialized_extra = str(extra)
    assert "hi" not in serialized_extra  # the plaintext message content
    assert "ok" not in serialized_extra  # the plaintext response content


async def test_spool_never_carries_plaintext_on_error_path_when_encrypted(
    messages: list[Message],
) -> None:
    """H1: the error path must also never leak plaintext to the spool."""
    inner = _inner()
    inner.invoke = AsyncMock(side_effect=RuntimeError("boom"))
    module = TelemetryModule(
        _config(
            store_raw_bodies=True,
            encryption={"enabled": True, "key_ref": "v1"},
            encryption_key_secret=_wrapping_key_secret(),
        ),
        inner,
    )
    recorded: list = []
    with patch.object(telemetry_mod, "_spool_record", recorded.append):
        with pytest.raises(RuntimeError):
            await module.invoke(messages)

    extra = recorded[0].extra
    assert "request_body" not in extra
    assert "hi" not in str(extra)


async def test_bodies_built_exactly_once_per_invoke(messages: list[Message]) -> None:
    """H1/M4: the trace_store record and the arcstore spool must share ONE
    body-building pass — previously each invoke() rebuilt+re-serialized
    the bodies a second time for the spool."""
    events: list = []
    module = TelemetryModule(_config(store_raw_bodies=True, on_event=events.append), _inner())

    calls = {"n": 0}
    real_raw_bodies = telemetry_mod.TelemetryModule._raw_bodies

    def _counting_raw_bodies(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return real_raw_bodies(self, *args, **kwargs)

    recorded: list = []
    with (
        patch.object(telemetry_mod.TelemetryModule, "_raw_bodies", _counting_raw_bodies),
        patch.object(telemetry_mod, "_spool_record", recorded.append),
    ):
        await module.invoke(messages)

    assert calls["n"] == 1
    assert events[0].request_body is not None
    assert recorded[0].extra["request_body"] is not None
