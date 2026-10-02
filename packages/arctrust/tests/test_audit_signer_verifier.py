"""Item 20 — WORM records name their signer; the chain verifies record by record.

``signer`` is the fingerprint of the key that signed the record. It is bound
into the record hash, so a forged or stripped signer fails verification rather
than mis-attributing the signature. ``ChainVerifier`` gives each record its own
verdict so a mirror can mark exactly the rows a break affects.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from arctrust import causal
from arctrust.audit import (
    AuditEvent,
    ChainVerifier,
    WormSink,
    signer_fingerprint,
    verify_chain,
)
from arctrust.keypair import KeyPair, generate_keypair
from arctrust.signer import InProcessSigner


def _event(i: int) -> AuditEvent:
    return AuditEvent(actor_did=f"did:arc:t:a/{i}", action="tool.call", target="t", outcome="ok")


def _chain(tmp_path: Path, n: int, *, max_records: int = 100_000) -> tuple[Path, KeyPair]:
    kp = generate_keypair()
    path = tmp_path / "audit-chain-a.jsonl"
    sink = WormSink(path, InProcessSigner(kp.private_key), max_records=max_records)
    for i in range(n):
        sink.write(_event(i))
    sink.close()
    return path, kp


def _lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _rewrite(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


class TestSigner:
    def test_every_record_names_its_signer(self, tmp_path: Path) -> None:
        path, kp = _chain(tmp_path, 2)
        assert {r["signer"] for r in _lines(path)} == {signer_fingerprint(kp.public_key)}
        assert verify_chain(path, kp.public_key)

    def test_a_forged_signer_fails_verification(self, tmp_path: Path) -> None:
        path, kp = _chain(tmp_path, 2)
        records = _lines(path)
        records[1]["signer"] = signer_fingerprint(generate_keypair().public_key)
        _rewrite(path, records)
        assert not verify_chain(path, kp.public_key)

    def test_a_stripped_signer_fails_verification(self, tmp_path: Path) -> None:
        path, kp = _chain(tmp_path, 2)
        records = _lines(path)
        del records[0]["signer"]
        _rewrite(path, records)
        assert not verify_chain(path, kp.public_key)

    def test_a_record_signed_by_another_key_fails(self, tmp_path: Path) -> None:
        path, _ = _chain(tmp_path, 1)
        assert not verify_chain(path, generate_keypair().public_key)

    def test_the_causal_context_is_written_outside_the_sealed_extra(self, tmp_path: Path) -> None:
        kp = generate_keypair()
        path = tmp_path / "audit-chain-a.jsonl"
        sink = WormSink(path, InProcessSigner(kp.private_key))
        with causal.bind(causal.root("agent", "did:arc:t:a/1")), causal.refine(run_id="r1"):
            sink.write(_event(0))
        sink.close()
        assert _lines(path)[0]["event"]["causal"]["run_id"] == "r1"
        assert verify_chain(path, kp.public_key)

    def test_a_forged_causal_context_fails_verification(self, tmp_path: Path) -> None:
        kp = generate_keypair()
        path = tmp_path / "audit-chain-a.jsonl"
        sink = WormSink(path, InProcessSigner(kp.private_key))
        with causal.bind(causal.root("agent", "did:arc:t:a/1")):
            sink.write(_event(0))
        sink.close()
        records = _lines(path)
        records[0]["event"]["causal"]["initiator"] = "operator"
        _rewrite(path, records)
        assert not verify_chain(path, kp.public_key)


class TestChainVerifier:
    def test_rows_before_a_break_stay_verified_and_rows_from_it_do_not(
        self, tmp_path: Path
    ) -> None:
        path, kp = _chain(tmp_path, 5)
        records = _lines(path)
        records[2]["event"]["outcome"] = "tampered"
        verifier = ChainVerifier(kp.public_key)
        verdicts = [verifier.check(r) for r in records]
        assert verdicts == [True, True, False, False, False]
        assert verifier.broken_at == 2

    def test_a_replayed_record_is_refused(self, tmp_path: Path) -> None:
        path, kp = _chain(tmp_path, 3)
        records = _lines(path)
        verifier = ChainVerifier(kp.public_key)
        assert [verifier.check(r) for r in records] == [True, True, True]
        assert not verifier.check(records[1])
        assert verifier.broken_at == 3

    def test_an_intact_chain_has_no_break(self, tmp_path: Path) -> None:
        path, kp = _chain(tmp_path, 3)
        verifier = ChainVerifier(kp.public_key)
        assert all(verifier.check(r) for r in _lines(path))
        assert verifier.broken_at is None
        assert verifier.next_seq == 3

    def test_a_malformed_record_is_a_break_not_a_crash(self, tmp_path: Path) -> None:
        _, kp = _chain(tmp_path, 1)
        verifier = ChainVerifier(kp.public_key)
        assert not verifier.check({"event": {}})
        assert verifier.broken_at == 0

    def test_rotated_segments_verify_as_one_chain(self, tmp_path: Path) -> None:
        path, kp = _chain(tmp_path, 5, max_records=2)
        assert len(list(tmp_path.glob("audit-chain-a.*.jsonl"))) == 2
        assert verify_chain(path, kp.public_key)
