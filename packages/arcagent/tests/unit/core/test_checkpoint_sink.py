"""SPEC-053 T-10 — trace checkpoint anchor is operator-signed; federal witnesses it.

The trace-store rotation checkpoint (arcllm ``build_checkpoint``) is anchored in
a WORM chain signed by the OPERATOR key, so ``read_verified_anchor`` proves the
head under the operator pubkey. At federal tier the operator-signed head is also
submitted to an external witness (REQ-009), so a rollback past the last anchor
is catchable even by a holder of the operator key.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arctrust import OperatorKey, read_verified_anchor

from arcagent.core import model_manager
from arcagent.core.model_manager import build_checkpoint_sink


def _checkpoint(head: str) -> dict[str, Any]:
    return {"head_hash": head, "record_count": 2, "files": ["traces-2026-07-06.jsonl"]}


def test_checkpoint_lands_in_operator_signed_worm(tmp_path: Path) -> None:
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    operator = OperatorKey.generate()

    sink = build_checkpoint_sink(
        agent_root, operator.into_signer(), actor_did="did:arc:test:exec/aa"
    )
    cp = _checkpoint("f" * 64)
    sink(cp)

    chain = agent_root / ".audit" / "trace-checkpoint.worm"
    anchor = read_verified_anchor(chain, operator.public_key)
    assert anchor is not None
    assert anchor["head_hash"] == "f" * 64


def test_federal_checkpoint_submitted_to_witness(tmp_path: Path) -> None:
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    operator = OperatorKey.generate()

    class _RecordingWitness:
        def __init__(self) -> None:
            self.submitted: list[tuple[dict[str, Any], bytes]] = []

        def submit(self, checkpoint: dict[str, Any], signature: bytes) -> str:
            self.submitted.append((checkpoint, signature))
            return str(checkpoint["head_hash"])

        def verify_inclusion(self, checkpoint: dict[str, Any], proof: str) -> bool:
            return True

    witness = _RecordingWitness()
    sink = build_checkpoint_sink(
        agent_root, operator.into_signer(), actor_did="did:arc:test:exec/aa", witness=witness
    )
    cp = _checkpoint("e" * 64)
    sink(cp)

    assert len(witness.submitted) == 1
    submitted_cp, sig = witness.submitted[0]
    assert submitted_cp["head_hash"] == "e" * 64
    assert len(sig) == 64  # Ed25519 operator signature over the checkpoint


def test_durable_anchor_failure_refuses_witness_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RecordingWitness:
        def __init__(self) -> None:
            self.calls = 0

        def submit(self, checkpoint: dict[str, Any], signature: bytes) -> str:
            self.calls += 1
            return "proof"

    def fail_write(_self: Any, _event: Any) -> None:
        raise OSError("audit disk unavailable")

    monkeypatch.setattr(model_manager.WormSink, "write_durable", fail_write)
    witness = RecordingWitness()
    signer = OperatorKey.generate().into_signer()
    sink = build_checkpoint_sink(
        tmp_path, signer, actor_did="did:arc:test:exec/aa", witness=witness
    )
    with pytest.raises(OSError, match="audit disk unavailable"):
        sink(_checkpoint("a" * 64))
    assert witness.calls == 0
    assert (
        read_verified_anchor(tmp_path / ".audit" / "trace-checkpoint.worm", signer.public_key)
        is None
    )


def test_anchor_readback_must_match_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    signer = OperatorKey.generate().into_signer()
    monkeypatch.setattr(model_manager, "read_verified_anchor", lambda *_args, **_kwargs: None)
    sink = build_checkpoint_sink(tmp_path, signer, actor_did="did:arc:test:exec/aa")
    with pytest.raises(RuntimeError, match="verification"):
        sink(_checkpoint("b" * 64))
