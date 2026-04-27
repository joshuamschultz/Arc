"""Tests for arctrust.audit — AuditEvent schema, JsonlSink, NullSink, emit, signed chain."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import pytest

from arctrust.audit import AuditEvent, JsonlSink, NullSink, SignedChainSink, emit
from arctrust.keypair import generate_keypair


# ---------------------------------------------------------------------------
# AuditEvent schema
# ---------------------------------------------------------------------------


class TestAuditEvent:
    def test_required_fields(self) -> None:
        evt = AuditEvent(
            actor_did="did:arc:test:exec/aabbccdd",
            action="tool.call",
            target="read_file",
            outcome="allow",
        )
        assert evt.actor_did == "did:arc:test:exec/aabbccdd"
        assert evt.action == "tool.call"
        assert evt.target == "read_file"
        assert evt.outcome == "allow"

    def test_optional_fields_have_defaults(self) -> None:
        evt = AuditEvent(
            actor_did="did:arc:test:exec/aabbccdd",
            action="policy.evaluate",
            target="tool_x",
            outcome="deny",
        )
        assert evt.classification is None
        assert evt.tier is None
        assert evt.request_id is None
        assert evt.payload_hash is None

    def test_ts_is_auto_populated(self) -> None:
        evt = AuditEvent(
            actor_did="did:arc:test:exec/aabbccdd",
            action="action",
            target="target",
            outcome="allow",
        )
        assert evt.ts is not None
        # ISO 8601 format
        assert "T" in evt.ts or "-" in evt.ts

    def test_frozen(self) -> None:
        evt = AuditEvent(
            actor_did="did:arc:test:exec/aabbccdd",
            action="action",
            target="target",
            outcome="allow",
        )
        with pytest.raises(Exception):
            evt.action = "modified"  # type: ignore[misc]

    def test_model_dump_returns_dict(self) -> None:
        evt = AuditEvent(
            actor_did="did:arc:test:exec/aabbccdd",
            action="action",
            target="target",
            outcome="allow",
        )
        d = evt.model_dump()
        assert isinstance(d, dict)
        assert d["action"] == "action"


# ---------------------------------------------------------------------------
# NullSink
# ---------------------------------------------------------------------------


class TestNullSink:
    def test_emit_is_noop(self) -> None:
        sink = NullSink()
        evt = AuditEvent(
            actor_did="did:arc:test:exec/aabbccdd",
            action="action",
            target="target",
            outcome="allow",
        )
        # Should not raise
        sink.write(evt)

    def test_records_is_empty(self) -> None:
        sink = NullSink()
        assert sink.records == []


# ---------------------------------------------------------------------------
# JsonlSink
# ---------------------------------------------------------------------------


class TestJsonlSink:
    def test_writes_valid_jsonl(self, tmp_path: Path) -> None:
        sink_path = tmp_path / "audit.jsonl"
        sink = JsonlSink(sink_path)
        evt = AuditEvent(
            actor_did="did:arc:test:exec/aabbccdd",
            action="tool.call",
            target="read_file",
            outcome="allow",
        )
        sink.write(evt)
        lines = sink_path.read_text().strip().split("\n")
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["action"] == "tool.call"
        assert parsed["actor_did"] == "did:arc:test:exec/aabbccdd"

    def test_appends_multiple_events(self, tmp_path: Path) -> None:
        sink_path = tmp_path / "audit.jsonl"
        sink = JsonlSink(sink_path)
        for i in range(3):
            evt = AuditEvent(
                actor_did=f"did:arc:test:exec/{i:08x}",
                action="action",
                target=f"target_{i}",
                outcome="allow",
            )
            sink.write(evt)
        lines = sink_path.read_text().strip().split("\n")
        assert len(lines) == 3

    def test_second_sink_on_same_path_appends(self, tmp_path: Path) -> None:
        sink_path = tmp_path / "audit.jsonl"
        for _ in range(2):
            sink = JsonlSink(sink_path)
            evt = AuditEvent(
                actor_did="did:arc:test:exec/aabbccdd",
                action="a",
                target="t",
                outcome="allow",
            )
            sink.write(evt)
        lines = sink_path.read_text().strip().split("\n")
        assert len(lines) == 2

    def test_in_memory_buffer(self) -> None:
        """JsonlSink can write to any file-like object (StringIO)."""
        buf = StringIO()
        sink = JsonlSink(buf)  # type: ignore[arg-type]
        evt = AuditEvent(
            actor_did="did:arc:test:exec/aabbccdd",
            action="x",
            target="y",
            outcome="deny",
        )
        sink.write(evt)
        content = buf.getvalue()
        assert "x" in content
        parsed = json.loads(content.strip())
        assert parsed["outcome"] == "deny"


# ---------------------------------------------------------------------------
# emit() function
# ---------------------------------------------------------------------------


class TestEmit:
    def test_emit_writes_to_sink(self, tmp_path: Path) -> None:
        sink_path = tmp_path / "emit_test.jsonl"
        sink = JsonlSink(sink_path)
        evt = AuditEvent(
            actor_did="did:arc:test:exec/aabbccdd",
            action="emit.test",
            target="tool",
            outcome="allow",
        )
        emit(evt, sink)
        content = sink_path.read_text()
        assert "emit.test" in content

    def test_emit_null_sink_noop(self) -> None:
        sink = NullSink()
        evt = AuditEvent(
            actor_did="did:arc:test:exec/aabbccdd",
            action="x",
            target="y",
            outcome="allow",
        )
        # Must not raise
        emit(evt, sink)

    def test_emit_sink_exception_does_not_propagate(self) -> None:
        """emit() must never raise even if the sink fails."""

        class BrokenSink:
            def write(self, event: AuditEvent) -> None:
                raise RuntimeError("sink exploded")

        evt = AuditEvent(
            actor_did="did:arc:test:exec/aabbccdd",
            action="x",
            target="y",
            outcome="allow",
        )
        # Should not propagate the RuntimeError
        emit(evt, BrokenSink())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# SignedChainSink — tamper evidence
# ---------------------------------------------------------------------------


class TestSignedChainSink:
    def test_chain_tip_changes_on_write(self) -> None:
        kp = generate_keypair()
        sink = SignedChainSink(operator_private_key=kp.private_key)
        initial_tip = sink.chain_tip

        evt = AuditEvent(
            actor_did="did:arc:test:exec/aabbccdd",
            action="a",
            target="t",
            outcome="allow",
        )
        sink.write(evt)
        assert sink.chain_tip != initial_tip

    def test_tampered_chain_detected(self) -> None:
        """Modifying a stored event's content breaks chain verification."""
        kp = generate_keypair()
        sink = SignedChainSink(operator_private_key=kp.private_key)

        for i in range(3):
            evt = AuditEvent(
                actor_did=f"did:arc:test:exec/{i:08x}",
                action="a",
                target="t",
                outcome="allow",
            )
            sink.write(evt)

        # Tamper: replace the event content inside the middle record.
        # This changes what verify_chain will hash, breaking the chain.
        assert len(sink.records) == 3
        original_record = sink.records[1]
        tampered_event = {**original_record["event"], "action": "tampered_action"}
        tampered_record = {**original_record, "event": tampered_event}
        sink.records[1] = tampered_record  # type: ignore[index]

        assert not sink.verify_chain()

    def test_intact_chain_verifies(self) -> None:
        kp = generate_keypair()
        sink = SignedChainSink(operator_private_key=kp.private_key)

        for i in range(5):
            evt = AuditEvent(
                actor_did=f"did:arc:test:exec/{i:08x}",
                action="action",
                target="target",
                outcome="allow",
            )
            sink.write(evt)

        assert sink.verify_chain()

    def test_empty_chain_verifies(self) -> None:
        kp = generate_keypair()
        sink = SignedChainSink(operator_private_key=kp.private_key)
        assert sink.verify_chain()
