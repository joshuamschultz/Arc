"""Tests for arctrust.audit — AuditEvent schema, NullSink, emit, durable WormSink."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arctrust.audit import AuditEvent, NullSink, WormSink, emit, verify_chain
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
        assert "T" in evt.ts or "-" in evt.ts

    def test_frozen(self) -> None:
        evt = AuditEvent(
            actor_did="did:arc:test:exec/aabbccdd",
            action="action",
            target="target",
            outcome="allow",
        )
        with pytest.raises(Exception):  # noqa: B017 — any exception on frozen-model mutation
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
        sink.write(evt)

    def test_records_is_empty(self) -> None:
        sink = NullSink()
        assert sink.records == []


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _evt(i: int = 0) -> AuditEvent:
    return AuditEvent(
        actor_did=f"did:arc:test:exec/{i:08x}",
        action="tool.call",
        target=f"target_{i}",
        outcome="allow",
    )


# ---------------------------------------------------------------------------
# emit() function
# ---------------------------------------------------------------------------


class TestEmit:
    def test_emit_writes_to_worm(self, tmp_path: Path) -> None:
        kp = generate_keypair()
        sink = WormSink(tmp_path / "audit.jsonl", kp.private_key)
        emit(_evt(), sink)
        assert sink.verify_chain()
        assert sink.chain_tip != ""

    def test_emit_null_sink_noop(self) -> None:
        emit(_evt(), NullSink())

    def test_emit_sink_exception_does_not_propagate(self) -> None:
        class BrokenSink:
            def write(self, event: AuditEvent) -> None:
                raise RuntimeError("sink exploded")

        emit(_evt(), BrokenSink())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# WormSink — durable, signed, hash-chained (FR-1)
# ---------------------------------------------------------------------------


class TestWormSink:
    def test_worm_persists_and_restores_chain_tip(self, tmp_path: Path) -> None:
        """AC-1.1 — chain survives process restart: fresh instance restores tip."""
        path = tmp_path / "audit.jsonl"
        kp = generate_keypair()

        sink = WormSink(path, kp.private_key)
        for i in range(5):
            sink.write(_evt(i))
        original_tip = sink.chain_tip
        del sink

        # Fresh instance over the same file restores the tip and verifies.
        restored = WormSink(path, kp.private_key)
        assert restored.chain_tip == original_tip
        assert restored.verify_chain()

        # Appending continues the same chain.
        restored.write(_evt(99))
        assert restored.chain_tip != original_tip
        assert restored.verify_chain()

    def test_empty_chain_verifies(self, tmp_path: Path) -> None:
        kp = generate_keypair()
        sink = WormSink(tmp_path / "audit.jsonl", kp.private_key)
        assert sink.verify_chain()
        assert sink.chain_tip == ""

    def test_intact_chain_verifies(self, tmp_path: Path) -> None:
        kp = generate_keypair()
        sink = WormSink(tmp_path / "audit.jsonl", kp.private_key)
        for i in range(8):
            sink.write(_evt(i))
        assert sink.verify_chain()

    def test_tampered_line_fails_verify(self, tmp_path: Path) -> None:
        """AC-1.2 — mutating any persisted byte breaks verification."""
        path = tmp_path / "audit.jsonl"
        kp = generate_keypair()
        sink = WormSink(path, kp.private_key)
        for i in range(3):
            sink.write(_evt(i))

        lines = path.read_text().splitlines()
        rec = json.loads(lines[1])
        rec["event"]["action"] = "tampered_action"
        lines[1] = json.dumps(rec)
        path.write_text("\n".join(lines) + "\n")

        assert not verify_chain(path, kp.public_key)

    def test_forged_signature_fails_verify(self, tmp_path: Path) -> None:
        """AC-1.3 / AC-1.6 — recomputed event_hash with an invalid signature fails."""
        path = tmp_path / "audit.jsonl"
        kp = generate_keypair()
        sink = WormSink(path, kp.private_key)
        for i in range(3):
            sink.write(_evt(i))

        # Forge: change the event AND recompute a consistent event_hash so the
        # hash-link check passes — only the Ed25519 signature can catch this.
        import hashlib

        lines = path.read_text().splitlines()
        rec = json.loads(lines[1])
        rec["event"]["action"] = "forged"
        payload = json.dumps(
            {"seq": rec["seq"], "prev_hash": rec["prev_hash"], "event": rec["event"]},
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
        )
        rec["event_hash"] = hashlib.sha256(payload.encode()).hexdigest()
        # signature left as-is → no longer matches the forged event_hash
        lines[1] = json.dumps(rec)
        path.write_text("\n".join(lines) + "\n")

        assert not verify_chain(path, kp.public_key)

    def test_verify_checks_signatures_not_just_links(self, tmp_path: Path) -> None:
        """AC-1.6 — a valid hash link with an invalid signature fails verification."""
        path = tmp_path / "audit.jsonl"
        kp = generate_keypair()
        sink = WormSink(path, kp.private_key)
        sink.write(_evt(0))

        lines = path.read_text().splitlines()
        rec = json.loads(lines[0])
        rec["signature"] = "00" * 64  # valid length, wrong signature
        lines[0] = json.dumps(rec)
        path.write_text("\n".join(lines) + "\n")

        assert not verify_chain(path, kp.public_key)

    def test_seq_gap_truncation_detected(self, tmp_path: Path) -> None:
        """AC-1.7 / C4 — removing a record creates a seq gap that verification catches."""
        path = tmp_path / "audit.jsonl"
        kp = generate_keypair()
        sink = WormSink(path, kp.private_key)
        for i in range(4):
            sink.write(_evt(i))

        lines = path.read_text().splitlines()
        del lines[1]  # remove seq=1 → gap
        path.write_text("\n".join(lines) + "\n")

        assert not verify_chain(path, kp.public_key)

    def test_emit_worm_fail_open(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """AC-1.4 — a write IO error is swallowed; emit() never raises."""
        kp = generate_keypair()
        sink = WormSink(tmp_path / "audit.jsonl", kp.private_key)

        def boom(*_a: object, **_k: object) -> int:
            raise OSError("disk full")

        monkeypatch.setattr("arctrust.audit.os.write", boom)
        emit(_evt(), sink)  # must not raise

    def test_file_is_owner_only_0600(self, tmp_path: Path) -> None:
        """NFR-5 — durable WORM file is created 0600."""
        path = tmp_path / "audit.jsonl"
        kp = generate_keypair()
        sink = WormSink(path, kp.private_key)
        sink.write(_evt())
        assert (path.stat().st_mode & 0o777) == 0o600

    def test_single_writer_enforced(self, tmp_path: Path) -> None:
        """C-2.11 — a second WormSink on the same active file is rejected (flock)."""
        path = tmp_path / "audit.jsonl"
        kp = generate_keypair()
        first = WormSink(path, kp.private_key)
        first.write(_evt())
        with pytest.raises(Exception):  # noqa: B017 — contention → raise
            WormSink(path, kp.private_key)

    def test_torn_line_recovery_and_signed_record(self, tmp_path: Path) -> None:
        """C-2.11 — a torn final line is truncated and a signed recovery record appended."""
        path = tmp_path / "audit.jsonl"
        kp = generate_keypair()
        sink = WormSink(path, kp.private_key)
        for i in range(3):
            sink.write(_evt(i))
        del sink

        # Simulate a crash mid-append: a partial, unterminated last line.
        with path.open("a", encoding="utf-8") as fh:
            fh.write('{"seq": 3, "event": {"actor_did": "x"')  # torn, no newline

        recovered = WormSink(path, kp.private_key)
        assert recovered.verify_chain()
        # The recovery is explicit, not silent: a recovery record was appended.
        actions = [json.loads(line)["event"]["action"] for line in path.read_text().splitlines()]
        assert "audit.worm.recovery" in actions

    def test_genesis_tip_anchored(self, tmp_path: Path) -> None:
        """C-2.13 — head replacement is caught against the expected genesis tip."""
        path = tmp_path / "audit.jsonl"
        kp = generate_keypair()
        sink = WormSink(path, kp.private_key)
        for i in range(3):
            sink.write(_evt(i))

        # Replace the head record's prev_hash with a different genesis.
        lines = path.read_text().splitlines()
        rec = json.loads(lines[0])
        rec["prev_hash"] = "f" * 64
        lines[0] = json.dumps(rec)
        path.write_text("\n".join(lines) + "\n")

        assert not verify_chain(path, kp.public_key)

    def test_rotation_keeps_chain_verifiable(self, tmp_path: Path) -> None:
        """C-2.12 — rotation across segments preserves one verifiable chain."""
        path = tmp_path / "audit.jsonl"
        kp = generate_keypair()
        sink = WormSink(path, kp.private_key, max_records=5)
        for i in range(17):
            sink.write(_evt(i))
        # Rotation produced extra segment files alongside the active file.
        segments = list(tmp_path.glob("audit.*.jsonl"))
        assert segments, "expected rotated segment files"
        assert sink.verify_chain()
        sink.close()
        # A fresh instance restores tip across segments and continues to verify.
        assert WormSink(path, kp.private_key).verify_chain()
