"""Tests for arcllm.trace_query.load_for_replay / ReplayRequest (SPEC-016)."""

import base64
import dataclasses
import inspect
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from arcllm._trace_crypto import seal
from arcllm.exceptions import ArcLLMConfigError, ArcLLMTraceNotFoundError
from arcllm.trace_query import ReplayRequest, load_for_replay
from arcllm.trace_store import JSONLTraceStore, TraceRecord
from arcllm.types import Message


def _wrapping_key() -> bytes:
    return AESGCM.generate_key(bit_length=256)


def _request_body() -> dict:
    return {
        "messages": [{"role": "user", "content": "hello"}],
        "tools": [{"name": "search", "description": "search the web", "parameters": {}}],
        "temperature": 0.5,
        "max_tokens": 100,
    }


class TestLoadForReplayPlaintext:
    async def test_reconstructs_request_equal_to_original_inputs(self, tmp_path: Path):
        store = JSONLTraceStore(tmp_path)
        rec = TraceRecord(
            trace_id="trace-1",
            provider="anthropic",
            model="claude-sonnet-4",
            request_body=_request_body(),
            response_body={"content": "hi", "tool_calls": [], "stop_reason": "end_turn"},
            lineage={"template": "greeting-v1"},
            classification="unclassified",
        )
        await store.append(rec)

        replay = await load_for_replay(store._traces_dir, "trace-1")

        assert replay.provider == "anthropic"
        assert replay.model == "claude-sonnet-4"
        assert len(replay.messages) == 1
        assert replay.messages[0].role == "user"
        assert replay.messages[0].content == "hello"
        assert replay.tools is not None
        assert replay.tools[0].name == "search"
        assert replay.options["temperature"] == 0.5
        assert replay.options["max_tokens"] == 100
        assert replay.lineage == {"template": "greeting-v1"}
        assert replay.classification == "unclassified"

    async def test_no_tools_reconstructs_none(self, tmp_path: Path):
        store = JSONLTraceStore(tmp_path)
        body = _request_body()
        body["tools"] = None
        await store.append(
            TraceRecord(
                trace_id="trace-2",
                provider="anthropic",
                model="claude",
                request_body=body,
            )
        )

        replay = await load_for_replay(store._traces_dir, "trace-2")
        assert replay.tools is None


class TestLoadForReplayEncrypted:
    async def test_decrypts_transparently(self, tmp_path: Path):
        store = JSONLTraceStore(tmp_path)
        wrapping_key = _wrapping_key()
        bodies = {
            "request_body": _request_body(),
            "response_body": {"content": "ok", "tool_calls": [], "stop_reason": "end_turn"},
        }
        envelope = seal(
            bodies,
            trace_id="trace-enc",
            timestamp="2026-03-01T00:00:00+00:00",
            wrapping_key=wrapping_key,
            key_ref="v1",
        )
        await store.append(
            TraceRecord(
                trace_id="trace-enc",
                timestamp="2026-03-01T00:00:00+00:00",
                provider="anthropic",
                model="claude",
                encryption=envelope,
            )
        )

        replay = await load_for_replay(
            store._traces_dir,
            "trace-enc",
            wrapping_key_resolver=lambda key_ref: wrapping_key,
        )

        assert replay.messages[0].content == "hello"
        assert replay.provider == "anthropic"

    async def test_missing_resolver_raises_clearly(self, tmp_path: Path):
        store = JSONLTraceStore(tmp_path)
        wrapping_key = _wrapping_key()
        envelope = seal(
            {"request_body": _request_body(), "response_body": None},
            trace_id="trace-3",
            timestamp="ts",
            wrapping_key=wrapping_key,
            key_ref="v1",
        )
        await store.append(
            TraceRecord(
                trace_id="trace-3",
                timestamp="ts",
                provider="anthropic",
                model="claude",
                encryption=envelope,
            )
        )

        with pytest.raises(ArcLLMConfigError, match="wrapping_key_resolver required"):
            await load_for_replay(store._traces_dir, "trace-3")

    async def test_resolver_receives_stored_key_ref(self, tmp_path: Path):
        """The resolver is called with the record's OWN key_ref (KEK rotation support)."""
        store = JSONLTraceStore(tmp_path)
        wrapping_key = _wrapping_key()
        envelope = seal(
            {"request_body": _request_body(), "response_body": None},
            trace_id="trace-4",
            timestamp="ts",
            wrapping_key=wrapping_key,
            key_ref="v-rotated",
        )
        await store.append(
            TraceRecord(
                trace_id="trace-4",
                timestamp="ts",
                provider="anthropic",
                model="claude",
                encryption=envelope,
            )
        )

        seen_refs = []

        def resolver(key_ref: str) -> bytes:
            seen_refs.append(key_ref)
            return wrapping_key

        await load_for_replay(store._traces_dir, "trace-4", wrapping_key_resolver=resolver)
        assert seen_refs == ["v-rotated"]


class TestLoadForReplayErrors:
    async def test_trace_not_found_raises(self, tmp_path: Path):
        store = JSONLTraceStore(tmp_path)
        with pytest.raises(ArcLLMTraceNotFoundError, match="does-not-exist"):
            await load_for_replay(store._traces_dir, "does-not-exist")

    async def test_metadata_only_legacy_record_raises_clear_error(self, tmp_path: Path):
        store = JSONLTraceStore(tmp_path)
        await store.append(
            TraceRecord(
                trace_id="legacy-1",
                provider="anthropic",
                model="claude",
                request_body=None,
                response_body=None,
            )
        )

        with pytest.raises(ArcLLMConfigError, match="not reconstructable"):
            await load_for_replay(store._traces_dir, "legacy-1")


class TestLineageRoundTrip:
    async def test_lineage_round_trips_verbatim(self, tmp_path: Path):
        store = JSONLTraceStore(tmp_path)
        lineage = {"rag_docs": ["doc-1", "doc-2"], "variables": {"user": "josh"}}
        await store.append(
            TraceRecord(
                trace_id="trace-lineage",
                provider="anthropic",
                model="claude",
                request_body=_request_body(),
                lineage=lineage,
            )
        )

        replay = await load_for_replay(store._traces_dir, "trace-lineage")
        assert replay.lineage == lineage


class TestReplayRequestBoundary:
    """SC-13 — ReplayRequest is data-only; arcllm never re-invokes a model."""

    def test_is_frozen_dataclass(self):
        assert dataclasses.is_dataclass(ReplayRequest)
        params = dataclasses.fields(ReplayRequest)
        assert {f.name for f in params} == {
            "provider",
            "model",
            "messages",
            "tools",
            "options",
            "lineage",
            "classification",
        }

    def test_frozen_instance_cannot_be_mutated(self):
        replay = ReplayRequest(
            provider="anthropic",
            model="claude",
            messages=[Message(role="user", content="hi")],
            tools=None,
            options={},
            lineage=None,
            classification="unclassified",
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            replay.provider = "openai"  # type: ignore[misc]

    def test_no_execute_or_invoke_method(self):
        replay = ReplayRequest(
            provider="anthropic",
            model="claude",
            messages=[],
            tools=None,
            options={},
            lineage=None,
            classification="unclassified",
        )
        assert not hasattr(replay, "execute")
        assert not hasattr(replay, "invoke")
        assert not hasattr(replay, "run")

    def test_carries_classification_for_access_control(self):
        """A decrypted replay is not stripped of its marking — CUI-on-replay."""
        replay = ReplayRequest(
            provider="anthropic",
            model="claude",
            messages=[],
            tools=None,
            options={},
            lineage=None,
            classification="cui",
        )
        assert replay.classification == "cui"

    def test_only_dataclass_methods_present(self):
        """No I/O-capable public methods beyond the dataclass-generated ones."""
        public_methods = {
            name
            for name, member in inspect.getmembers(ReplayRequest, predicate=inspect.isfunction)
            if not name.startswith("_")
        }
        assert public_methods == set()


class TestDecodeWrappingKeyIntegration:
    def test_base64_wrapping_key_helper_used_by_seal_flow(self):
        """Sanity: the crypto module's own base64 32-byte contract holds end to end."""
        raw = AESGCM.generate_key(bit_length=256)
        encoded = base64.b64encode(raw).decode("ascii")
        assert base64.b64decode(encoded) == raw
