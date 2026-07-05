"""Tests for SPEC-016 full trace capture: default flip, classification,
lineage, encryption wiring, size cap, and the tier-through-construction
regression (AU-2). Complements the pre-existing tests in test_telemetry.py
and test_trace_store.py, which already cover the base schema/hash-chain
and were updated in place for the default flip.
"""

import base64
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from arcllm.exceptions import ArcLLMConfigError
from arcllm.modules.telemetry import TelemetryModule, resolve_classification
from arcllm.trace_store import EncryptedEnvelope, JSONLTraceStore, TraceRecord
from arcllm.types import LLMProvider, LLMResponse, Message, Usage

_OK_RESPONSE = LLMResponse(
    content="ok",
    usage=Usage(input_tokens=100, output_tokens=50, total_tokens=150),
    model="test-model",
    stop_reason="end_turn",
)


def _make_inner(name: str = "anthropic") -> MagicMock:
    inner = MagicMock(spec=LLMProvider)
    inner.name = name
    inner.model_name = "test-model"
    inner.validate_config.return_value = True
    inner.invoke = AsyncMock(return_value=_OK_RESPONSE)
    return inner


def _wrapping_key_secret() -> str:
    return base64.b64encode(AESGCM.generate_key(bit_length=256)).decode("ascii")


@pytest.fixture
def messages():
    return [Message(role="user", content="hi")]


# ---------------------------------------------------------------------------
# Schema — new TraceRecord fields (T16.1)
# ---------------------------------------------------------------------------


class TestTraceRecordNewFields:
    def test_classification_defaults_to_unclassified(self):
        rec = TraceRecord(provider="anthropic", model="claude")
        assert rec.classification == "unclassified"

    def test_encryption_defaults_to_none(self):
        rec = TraceRecord(provider="anthropic", model="claude")
        assert rec.encryption is None

    def test_lineage_defaults_to_none(self):
        rec = TraceRecord(provider="anthropic", model="claude")
        assert rec.lineage is None

    def test_legacy_record_without_new_fields_still_parses(self):
        """Records written before SPEC-016 (no classification/encryption/lineage) still parse."""
        legacy_data = {
            "trace_id": "old-1",
            "timestamp": "2025-01-01T00:00:00+00:00",
            "provider": "anthropic",
            "model": "claude",
        }
        rec = TraceRecord(**legacy_data)
        assert rec.classification == "unclassified"
        assert rec.encryption is None
        assert rec.lineage is None

    def test_encryption_field_accepts_envelope(self):
        envelope = EncryptedEnvelope(
            wrapped_key="a", key_ref="v1", nonce="b", ciphertext="c", aad="d"
        )
        rec = TraceRecord(provider="anthropic", model="claude", encryption=envelope)
        assert rec.encryption == envelope

    def test_hash_covers_classification(self):
        base = TraceRecord(provider="anthropic", model="claude")
        modified = base.model_copy(update={"classification": "cui"})
        assert base.compute_hash() != modified.compute_hash()

    def test_hash_covers_lineage(self):
        base = TraceRecord(provider="anthropic", model="claude")
        modified = base.model_copy(update={"lineage": {"a": 1}})
        assert base.compute_hash() != modified.compute_hash()

    def test_hash_covers_encryption(self):
        base = TraceRecord(provider="anthropic", model="claude")
        envelope = EncryptedEnvelope(
            wrapped_key="a", key_ref="v1", nonce="b", ciphertext="c", aad="d"
        )
        modified = base.model_copy(update={"encryption": envelope})
        assert base.compute_hash() != modified.compute_hash()


# ---------------------------------------------------------------------------
# Default flip + audited disable (T16.4) — SC-1, SC-2, SC-3, SC-15
# ---------------------------------------------------------------------------


class TestDefaultFlip:
    async def test_no_config_override_populates_bodies(self, messages):
        """SC-1 — a call with no config produces populated request/response bodies."""
        events: list[TraceRecord] = []
        module = TelemetryModule({"on_event": events.append}, _make_inner())
        await module.invoke(messages)
        rec = events[0]
        assert rec.request_body is not None
        assert rec.response_body is not None

    async def test_request_body_completeness(self, messages):
        """SC-2 — request_body captures messages, model, provider, tools, options."""
        events: list[TraceRecord] = []
        module = TelemetryModule({"on_event": events.append}, _make_inner())
        tools = None
        await module.invoke(messages, tools, temperature=0.3, max_tokens=50)
        rec = events[0]
        assert rec.request_body["messages"][0]["content"] == "hi"
        assert rec.request_body["temperature"] == 0.3
        assert rec.request_body["max_tokens"] == 50

    async def test_response_body_completeness(self, messages):
        """SC-3 — response_body captures content, tool_calls, stop_reason."""
        events: list[TraceRecord] = []
        module = TelemetryModule({"on_event": events.append}, _make_inner())
        await module.invoke(messages)
        rec = events[0]
        assert rec.response_body["content"] == "ok"
        assert rec.response_body["tool_calls"] == []
        assert rec.response_body["stop_reason"] == "end_turn"


# ---------------------------------------------------------------------------
# Classification floor (D-439)
# ---------------------------------------------------------------------------


class TestClassificationFloor:
    def test_resolve_classification_no_request_uses_floor(self):
        assert resolve_classification(None, "cui") == "cui"

    def test_resolve_classification_above_floor_kept(self):
        assert resolve_classification("secret", "cui") == "secret"

    def test_resolve_classification_below_floor_clamped(self):
        assert resolve_classification("unclassified", "cui") == "cui"

    def test_resolve_classification_unrecognized_clamped_to_floor(self):
        """An unverifiable label can never silently satisfy the floor (fail-safe)."""
        assert resolve_classification("bogus-level", "cui") == "cui"

    async def test_per_call_classification_above_floor_persists(self, messages):
        events: list[TraceRecord] = []
        module = TelemetryModule(
            {"on_event": events.append, "classification": "cui"}, _make_inner()
        )
        await module.invoke(messages, classification="secret")
        assert events[0].classification == "secret"

    async def test_per_call_classification_below_floor_never_downgrades(self, messages):
        events: list[TraceRecord] = []
        module = TelemetryModule(
            {"on_event": events.append, "classification": "secret"}, _make_inner()
        )
        await module.invoke(messages, classification="unclassified")
        assert events[0].classification == "secret"

    async def test_default_floor_applied_when_no_per_call_value(self, messages):
        events: list[TraceRecord] = []
        module = TelemetryModule(
            {"on_event": events.append, "classification": "cui"}, _make_inner()
        )
        await module.invoke(messages)
        assert events[0].classification == "cui"

    async def test_classification_kwarg_not_forwarded_to_request_body(self, messages):
        """classification must not leak into the captured request_body payload."""
        events: list[TraceRecord] = []
        module = TelemetryModule({"on_event": events.append}, _make_inner())
        await module.invoke(messages, classification="cui")
        assert "classification" not in events[0].request_body


# ---------------------------------------------------------------------------
# Lineage (D-443) — verbatim persistence
# ---------------------------------------------------------------------------


class TestLineage:
    async def test_per_call_lineage_persisted_verbatim(self, messages):
        events: list[TraceRecord] = []
        module = TelemetryModule({"on_event": events.append}, _make_inner())
        lineage = {"template": "v2", "rag_docs": ["a", "b"]}
        await module.invoke(messages, lineage=lineage)
        assert events[0].lineage == lineage

    async def test_config_level_lineage_default(self, messages):
        events: list[TraceRecord] = []
        default_lineage = {"source": "static-config"}
        module = TelemetryModule(
            {"on_event": events.append, "lineage": default_lineage}, _make_inner()
        )
        await module.invoke(messages)
        assert events[0].lineage == default_lineage

    async def test_per_call_lineage_overrides_config_default(self, messages):
        events: list[TraceRecord] = []
        module = TelemetryModule(
            {"on_event": events.append, "lineage": {"source": "default"}}, _make_inner()
        )
        await module.invoke(messages, lineage={"source": "override"})
        assert events[0].lineage == {"source": "override"}

    async def test_no_lineage_is_none(self, messages):
        events: list[TraceRecord] = []
        module = TelemetryModule({"on_event": events.append}, _make_inner())
        await module.invoke(messages)
        assert events[0].lineage is None

    async def test_lineage_not_forwarded_to_inner_provider(self, messages):
        inner = _make_inner()
        module = TelemetryModule({}, inner)
        await module.invoke(messages, lineage={"a": 1})
        _, call_kwargs = inner.invoke.call_args
        assert "lineage" not in call_kwargs

    async def test_lineage_not_leaked_into_request_body(self, messages):
        events: list[TraceRecord] = []
        module = TelemetryModule({"on_event": events.append}, _make_inner())
        await module.invoke(messages, lineage={"a": 1})
        assert "lineage" not in events[0].request_body

    async def test_oversized_lineage_truncated_with_marker(self, messages):
        events: list[TraceRecord] = []
        module = TelemetryModule({"on_event": events.append}, _make_inner())
        huge_lineage = {"blob": "x" * 20_000}
        await module.invoke(messages, lineage=huge_lineage)
        rec_lineage = events[0].lineage
        assert rec_lineage["truncated"] is True
        assert rec_lineage["original_bytes"] > 8192


# ---------------------------------------------------------------------------
# Per-body size cap (FR-23)
# ---------------------------------------------------------------------------


class TestBodySizeCap:
    async def test_oversized_request_body_truncated(self, messages):
        events: list[TraceRecord] = []
        module = TelemetryModule({"on_event": events.append, "max_body_bytes": 100}, _make_inner())
        huge_messages = [Message(role="user", content="x" * 10_000)]
        await module.invoke(huge_messages)
        assert events[0].request_body["truncated"] is True
        assert events[0].request_body["original_bytes"] > 100

    async def test_body_under_cap_not_truncated(self, messages):
        events: list[TraceRecord] = []
        module = TelemetryModule(
            {"on_event": events.append, "max_body_bytes": 1_000_000}, _make_inner()
        )
        await module.invoke(messages)
        assert "truncated" not in events[0].request_body

    async def test_oversized_list_content_request_body_truncated_via_cheap_hint(self):
        """M4: the cheap size hint must also count list[ContentBlock] text,
        not just plain string message content."""
        from arcllm.types import TextBlock

        events: list[TraceRecord] = []
        module = TelemetryModule({"on_event": events.append, "max_body_bytes": 100}, _make_inner())
        huge_messages = [
            Message(role="user", content=[TextBlock(text="x" * 10_000)]),
        ]
        await module.invoke(huge_messages)
        assert events[0].request_body["truncated"] is True
        assert events[0].request_body["original_bytes"] > 100

    async def test_oversized_response_tool_call_arguments_truncated_via_cheap_hint(self):
        """M4: the response cheap size hint must also count tool_calls arguments."""
        from arcllm.types import ToolCall

        events: list[TraceRecord] = []
        inner = _make_inner()
        inner.invoke = AsyncMock(
            return_value=_OK_RESPONSE.model_copy(
                update={
                    "tool_calls": [ToolCall(id="1", name="big", arguments={"blob": "x" * 10_000})]
                }
            )
        )
        module = TelemetryModule({"on_event": events.append, "max_body_bytes": 100}, inner)
        await module.invoke([Message(role="user", content="hi")])
        assert events[0].response_body["truncated"] is True
        assert events[0].response_body["original_bytes"] > 100


# ---------------------------------------------------------------------------
# Encryption wiring (T16.6) — SC-5
# ---------------------------------------------------------------------------


class TestEncryptionWiring:
    async def test_encrypted_record_has_null_bodies_and_envelope(self, messages):
        events: list[TraceRecord] = []
        module = TelemetryModule(
            {
                "on_event": events.append,
                "encryption": {"enabled": True, "key_ref": "v1"},
                "encryption_key_secret": _wrapping_key_secret(),
            },
            _make_inner(),
        )
        await module.invoke(messages)
        rec = events[0]
        assert rec.request_body is None
        assert rec.response_body is None
        assert rec.encryption is not None
        assert rec.encryption.key_ref == "v1"

    async def test_encryption_enabled_without_secret_fails_closed(self):
        with pytest.raises(ArcLLMConfigError, match="encryption_key_secret"):
            TelemetryModule({"encryption": {"enabled": True, "key_ref": "v1"}}, _make_inner())

    async def test_encryption_enabled_with_malformed_secret_fails_closed(self):
        with pytest.raises(ArcLLMConfigError):
            TelemetryModule(
                {
                    "encryption": {"enabled": True, "key_ref": "v1"},
                    "encryption_key_secret": "not-valid-base64!!!",
                },
                _make_inner(),
            )

    async def test_verify_chain_passes_over_encrypted_records(self, messages, tmp_path: Path):
        store = JSONLTraceStore(tmp_path)
        module = TelemetryModule(
            {
                "trace_store": store,
                "encryption": {"enabled": True, "key_ref": "v1"},
                "encryption_key_secret": _wrapping_key_secret(),
            },
            _make_inner(),
        )
        await module.invoke(messages)
        await module.invoke(messages)
        assert await store.verify_chain() is True

    async def test_encryption_disabled_by_default_never_seals(self, messages):
        events: list[TraceRecord] = []
        module = TelemetryModule({"on_event": events.append}, _make_inner())
        await module.invoke(messages)
        assert events[0].encryption is None
        assert events[0].request_body is not None

    def test_require_fips_true_fails_closed_without_fips_provider(self):
        with pytest.raises(ArcLLMConfigError, match="FIPS-140-3-approved"):
            TelemetryModule(
                {
                    "encryption": {
                        "enabled": True,
                        "key_ref": "v1",
                        "require_fips": True,
                    },
                    "encryption_key_secret": _wrapping_key_secret(),
                },
                _make_inner(),
            )

    def test_invalid_encryption_config_key_rejected(self):
        with pytest.raises(ArcLLMConfigError, match="Invalid telemetry encryption config"):
            TelemetryModule({"encryption": {"nonsense_key": True}}, _make_inner())


# ---------------------------------------------------------------------------
# AU-2 — tier posture flows through construction, never per-call (Research
# Insight; mirrors test_registry_propagates_tier_to_policy_context in
# arcagent, adapted to arcllm's own construction-time resolution).
# ---------------------------------------------------------------------------


class TestTierFlowsThroughConstruction:
    async def test_encryption_posture_is_fixed_at_construction_not_per_call(self, messages):
        """No per-call kwarg can flip encryption on/off — it's construction-only."""
        events: list[TraceRecord] = []
        module = TelemetryModule(
            {
                "on_event": events.append,
                "encryption": {"enabled": True, "key_ref": "v1"},
                "encryption_key_secret": _wrapping_key_secret(),
            },
            _make_inner(),
        )
        # No per-call kwarg exists to disable encryption; every call this
        # instance ever makes seals bodies, regardless of call-site kwargs.
        await module.invoke(messages, encryption=False)  # type: ignore[call-arg]
        assert events[0].encryption is not None

    async def test_classification_floor_is_fixed_at_construction(self, messages):
        """The floor itself cannot be raised or lowered from a per-call kwarg."""
        events: list[TraceRecord] = []
        module = TelemetryModule(
            {"on_event": events.append, "classification": "cui"}, _make_inner()
        )
        for _ in range(3):
            await module.invoke(messages)
        assert all(e.classification == "cui" for e in events)

    async def test_store_raw_bodies_posture_stable_across_many_calls(self, messages):
        """The construction-time capture posture never drifts across calls."""
        events: list[TraceRecord] = []
        module = TelemetryModule(
            {"on_event": events.append, "store_raw_bodies": True}, _make_inner()
        )
        for _ in range(5):
            await module.invoke(messages)
        assert all(e.request_body is not None for e in events)


# ---------------------------------------------------------------------------
# Audited disable — FR-21 / D-444 (additional edge cases beyond
# test_telemetry.py's core coverage)
# ---------------------------------------------------------------------------


class TestAuditedDisableEdgeCases:
    async def test_disable_audit_fires_before_first_llm_call_record(self, messages):
        events: list[TraceRecord] = []
        module = TelemetryModule(
            {"on_event": events.append, "store_raw_bodies": False}, _make_inner()
        )
        await module.invoke(messages)
        assert events[0].event_type == "config_change"
        assert events[1].event_type == "llm_call"

    async def test_disable_audit_never_repeats_on_same_instance(self, messages):
        events: list[TraceRecord] = []
        module = TelemetryModule(
            {"on_event": events.append, "store_raw_bodies": False}, _make_inner()
        )
        for _ in range(4):
            await module.invoke(messages)
        config_changes = [e for e in events if e.event_type == "config_change"]
        assert len(config_changes) == 1

    async def test_new_instance_with_disable_gets_its_own_audit_record(self, messages):
        """Each disabled instance is independently audited (no shared global state)."""
        events_a: list[TraceRecord] = []
        events_b: list[TraceRecord] = []
        module_a = TelemetryModule(
            {"on_event": events_a.append, "store_raw_bodies": False}, _make_inner()
        )
        module_b = TelemetryModule(
            {"on_event": events_b.append, "store_raw_bodies": False}, _make_inner()
        )
        await module_a.invoke(messages)
        await module_b.invoke(messages)
        assert len([e for e in events_a if e.event_type == "config_change"]) == 1
        assert len([e for e in events_b if e.event_type == "config_change"]) == 1
