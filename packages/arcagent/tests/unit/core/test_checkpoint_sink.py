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

from arctrust import OperatorKey, read_verified_anchor

from arcagent.core.model_manager import build_checkpoint_sink


def _checkpoint(head: str) -> dict[str, Any]:
    return {"head_hash": head, "record_count": 2, "files": ["traces-2026-07-06.jsonl"]}


def test_checkpoint_lands_in_operator_signed_worm(tmp_path: Path) -> None:
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    operator = OperatorKey.generate()

    sink = build_checkpoint_sink(agent_root, operator.into_signer(), actor_did="did:arc:test:exec/aa")
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
