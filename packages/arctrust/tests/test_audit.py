"""Tests for arctrust.audit — AuditEvent schema, NullSink, emit, durable WormSink."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arctrust.audit import AuditEvent, NullSink, WormSink, emit, read_verified_anchor, verify_chain
from arctrust.keypair import KeyPair, generate_keypair
from arctrust.keypair import sign as _kp_sign
from arctrust.signer import InProcessSigner

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
        sink = WormSink(tmp_path / "audit.jsonl", InProcessSigner(kp.private_key))
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

        sink = WormSink(path, InProcessSigner(kp.private_key))
        for i in range(5):
            sink.write(_evt(i))
        original_tip = sink.chain_tip
        del sink

        # Fresh instance over the same file restores the tip and verifies.
        restored = WormSink(path, InProcessSigner(kp.private_key))
        assert restored.chain_tip == original_tip
        assert restored.verify_chain()

        # Appending continues the same chain.
        restored.write(_evt(99))
        assert restored.chain_tip != original_tip
        assert restored.verify_chain()

    def test_empty_chain_verifies(self, tmp_path: Path) -> None:
        kp = generate_keypair()
        sink = WormSink(tmp_path / "audit.jsonl", InProcessSigner(kp.private_key))
        assert sink.verify_chain()
        assert sink.chain_tip == ""

    def test_intact_chain_verifies(self, tmp_path: Path) -> None:
        kp = generate_keypair()
        sink = WormSink(tmp_path / "audit.jsonl", InProcessSigner(kp.private_key))
        for i in range(8):
            sink.write(_evt(i))
        assert sink.verify_chain()

    def test_tampered_line_fails_verify(self, tmp_path: Path) -> None:
        """AC-1.2 — mutating any persisted byte breaks verification."""
        path = tmp_path / "audit.jsonl"
        kp = generate_keypair()
        sink = WormSink(path, InProcessSigner(kp.private_key))
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
        sink = WormSink(path, InProcessSigner(kp.private_key))
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
        sink = WormSink(path, InProcessSigner(kp.private_key))
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
        sink = WormSink(path, InProcessSigner(kp.private_key))
        for i in range(4):
            sink.write(_evt(i))

        lines = path.read_text().splitlines()
        del lines[1]  # remove seq=1 → gap
        path.write_text("\n".join(lines) + "\n")

        assert not verify_chain(path, kp.public_key)

    def test_emit_worm_fail_open(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """AC-1.4 — a write IO error is swallowed; emit() never raises."""
        kp = generate_keypair()
        sink = WormSink(tmp_path / "audit.jsonl", InProcessSigner(kp.private_key))

        def boom(*_a: object, **_k: object) -> int:
            raise OSError("disk full")

        monkeypatch.setattr("arctrust.audit.os.write", boom)
        emit(_evt(), sink)  # must not raise

    def test_file_is_owner_only_0600(self, tmp_path: Path) -> None:
        """NFR-5 — durable WORM file is created 0600."""
        path = tmp_path / "audit.jsonl"
        kp = generate_keypair()
        sink = WormSink(path, InProcessSigner(kp.private_key))
        sink.write(_evt())
        assert (path.stat().st_mode & 0o777) == 0o600

    def test_single_writer_enforced(self, tmp_path: Path) -> None:
        """C-2.11 — a second WormSink on the same active file is rejected (flock)."""
        path = tmp_path / "audit.jsonl"
        kp = generate_keypair()
        first = WormSink(path, InProcessSigner(kp.private_key))
        first.write(_evt())
        with pytest.raises(Exception):  # noqa: B017 — contention → raise
            WormSink(path, InProcessSigner(kp.private_key))

    def test_torn_line_recovery_and_signed_record(self, tmp_path: Path) -> None:
        """C-2.11 — a torn final line is truncated and a signed recovery record appended."""
        path = tmp_path / "audit.jsonl"
        kp = generate_keypair()
        sink = WormSink(path, InProcessSigner(kp.private_key))
        for i in range(3):
            sink.write(_evt(i))
        del sink

        # Simulate a crash mid-append: a partial, unterminated last line.
        with path.open("a", encoding="utf-8") as fh:
            fh.write('{"seq": 3, "event": {"actor_did": "x"')  # torn, no newline

        recovered = WormSink(path, InProcessSigner(kp.private_key))
        assert recovered.verify_chain()
        # The recovery is explicit, not silent: a recovery record was appended.
        actions = [json.loads(line)["event"]["action"] for line in path.read_text().splitlines()]
        assert "audit.worm.recovery" in actions

    def test_genesis_tip_anchored(self, tmp_path: Path) -> None:
        """C-2.13 — head replacement is caught against the expected genesis tip."""
        path = tmp_path / "audit.jsonl"
        kp = generate_keypair()
        sink = WormSink(path, InProcessSigner(kp.private_key))
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
        sink = WormSink(path, InProcessSigner(kp.private_key), max_records=5)
        for i in range(17):
            sink.write(_evt(i))
        # Rotation produced extra segment files alongside the active file.
        segments = list(tmp_path.glob("audit.*.jsonl"))
        assert segments, "expected rotated segment files"
        assert sink.verify_chain()
        sink.close()
        # A fresh instance restores tip across segments and continues to verify.
        assert WormSink(path, InProcessSigner(kp.private_key)).verify_chain()


# ---------------------------------------------------------------------------
# read_verified_anchor() — trace-checkpoint signed anchor (SPEC: arcllm gap)
# ---------------------------------------------------------------------------


def _checkpoint_evt(i: int, head_hash: str) -> AuditEvent:
    return AuditEvent(
        actor_did=f"did:arc:test:exec/{i:08x}",
        action="trace.checkpoint",
        target="traces",
        outcome="allow",
        extra={
            "head_hash": head_hash,
            "record_count": i,
            "files": [f"traces-2026-01-{i:02d}.jsonl"],
        },
    )


class TestChainAnchor:
    def test_happy_path_returns_newest_checkpoint_extra(self, tmp_path: Path) -> None:
        """Two checkpoints emitted; the newest one's extra is returned."""
        path = tmp_path / "audit.jsonl"
        kp = generate_keypair()
        sink = WormSink(path, InProcessSigner(kp.private_key))
        emit(_checkpoint_evt(1, "a" * 64), sink)
        emit(_checkpoint_evt(2, "b" * 64), sink)

        anchor = read_verified_anchor(path, kp.public_key)

        assert anchor is not None
        assert anchor["head_hash"] == "b" * 64
        assert anchor["record_count"] == 2

    def test_returns_none_when_chain_tampered(self, tmp_path: Path) -> None:
        """A mutated line fails verify_chain(), so no anchor can be trusted."""
        path = tmp_path / "audit.jsonl"
        kp = generate_keypair()
        sink = WormSink(path, InProcessSigner(kp.private_key))
        emit(_checkpoint_evt(1, "a" * 64), sink)

        lines = path.read_text().splitlines()
        rec = json.loads(lines[0])
        rec["event"]["target"] = "tampered"
        path.write_text(json.dumps(rec) + "\n")

        assert read_verified_anchor(path, kp.public_key) is None

    def test_returns_none_when_no_checkpoint_action_present(self, tmp_path: Path) -> None:
        """A verifiable chain with no matching action yields no anchor."""
        path = tmp_path / "audit.jsonl"
        kp = generate_keypair()
        sink = WormSink(path, InProcessSigner(kp.private_key))
        emit(_evt(0), sink)  # action="tool.call" — not a checkpoint

        assert read_verified_anchor(path, kp.public_key) is None

    def test_ignores_non_checkpoint_events_among_mixed(self, tmp_path: Path) -> None:
        """Non-checkpoint events interleaved with checkpoints are skipped."""
        path = tmp_path / "audit.jsonl"
        kp = generate_keypair()
        sink = WormSink(path, InProcessSigner(kp.private_key))
        emit(_checkpoint_evt(1, "a" * 64), sink)
        emit(_evt(1), sink)
        emit(_checkpoint_evt(2, "c" * 64), sink)
        emit(_evt(2), sink)

        anchor = read_verified_anchor(path, kp.public_key)

        assert anchor is not None
        assert anchor["head_hash"] == "c" * 64


# ---------------------------------------------------------------------------
# SPEC-053 T-03 — audit authority is the OPERATOR key, never an agent DID
# ---------------------------------------------------------------------------


class TestAuditAuthorityIndependence:
    def test_chain_signed_by_operator_not_agent(self, tmp_path: Path) -> None:
        """A WORM chain signed with the operator seed verifies ONLY under the
        operator public key; the agent DID key — even a valid one — must fail.

        This is the whole point of SPEC-053: the audited subject (agent) is not
        the audit authority (operator). If the agent key verified the chain, a
        compromised agent could re-sign its own tamper-evident history.
        """
        from arctrust.operator import OperatorKey

        operator = OperatorKey.generate()
        agent = generate_keypair()  # a legitimate, distinct agent DID keypair

        chain = tmp_path / "audit" / "policy.worm"
        sink = WormSink(chain, InProcessSigner(operator.seed))
        for i in range(3):
            sink.write(
                AuditEvent(
                    actor_did="did:arc:test:exec/aabbccdd",
                    action="policy.evaluate",
                    target=f"tool-{i}",
                    outcome="allow",
                )
            )
        sink.close()

        assert verify_chain(chain, operator.public_key) is True
        assert verify_chain(chain, agent.public_key) is False
        assert operator.public_key != agent.public_key


class TestOutOfProcessAuditCustody:
    """SPEC-037 REQ-006 — the WORM chain signs by reference under vault-transit;
    the operator seed NEVER materialises in the agent process."""

    def test_chain_signed_by_reference_seed_never_materialises(self, tmp_path: Path) -> None:
        from arctrust.signer import VaultSigner

        operator = generate_keypair()

        class _SeedGuardTransit:
            """Signs by reference; raises if anyone reaches for the raw seed."""

            def __init__(self, seed: bytes) -> None:
                self._kp = KeyPair.from_seed(seed)
                self.sign_calls = 0

            def sign(self, key_ref: str, message: bytes) -> bytes:
                self.sign_calls += 1
                return _kp_sign(message, self._kp.private_key)

            def public_key(self, key_ref: str) -> bytes:
                return self._kp.public_key

            @property
            def seed(self) -> bytes:  # pragma: no cover - must never be reached
                raise AssertionError("WORM sink reached for the operator seed (REQ-006)")

        transit = _SeedGuardTransit(operator.private_key)
        chain = tmp_path / "audit" / "policy.worm"
        sink = WormSink(chain, VaultSigner(transit, key_ref="operator"))
        for i in range(3):
            sink.write(
                AuditEvent(
                    actor_did="did:arc:test:exec/aabbccdd",
                    action="policy.evaluate",
                    target=f"tool-{i}",
                    outcome="allow",
                )
            )
        sink.close()

        # The chain verifies under the operator public key the transit exposed,
        # and every record was signed via the out-of-process boundary.
        assert verify_chain(chain, operator.public_key) is True
        assert transit.sign_calls == 3
