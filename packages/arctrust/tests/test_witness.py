"""SPEC-053 T-04 — external witness anchor (federal REQ-009/010).

A holder of the operator key can re-sign a local WORM chain, but cannot
retroactively insert a forged head into an append-only log they do not
control. The witness is that external log. Two implementations, selected by
config: an offline/air-gapped append-only medium (Must) and an online
transparency-log/Rekor-style submitter (Should).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from arctrust.witness import (
    AppendOnlyMediumWitness,
    TransparencyLogWitness,
    WitnessAnchor,
    WitnessDivergenceError,
    verify_local_head_witnessed,
)


def _checkpoint(head: str) -> dict[str, Any]:
    return {"head_hash": head, "record_count": 3, "files": ["traces-2026-07-06.jsonl"]}


def test_append_only_medium_submit_appends_and_returns_proof(tmp_path: Path) -> None:
    medium = tmp_path / "witness" / "anchor.log"
    witness = AppendOnlyMediumWitness(medium)
    proof = witness.submit(_checkpoint("a" * 64), signature=b"\x01" * 64)
    assert proof
    assert medium.exists()
    # A second, separately-custodied append-only file now holds the head.
    assert "a" * 64 in medium.read_text()


def test_append_only_medium_verify_inclusion(tmp_path: Path) -> None:
    medium = tmp_path / "witness" / "anchor.log"
    witness = AppendOnlyMediumWitness(medium)
    cp = _checkpoint("b" * 64)
    proof = witness.submit(cp, signature=b"\x02" * 64)

    assert witness.verify_inclusion(cp, proof) is True
    # A head that was never submitted is not attested by the witness.
    assert witness.verify_inclusion(_checkpoint("c" * 64), proof) is False


def test_append_only_medium_is_append_only(tmp_path: Path) -> None:
    medium = tmp_path / "witness" / "anchor.log"
    witness = AppendOnlyMediumWitness(medium)
    witness.submit(_checkpoint("d" * 64), signature=b"\x03" * 64)
    witness.submit(_checkpoint("e" * 64), signature=b"\x04" * 64)
    # Both heads persist — submission never rewrites earlier entries.
    text = medium.read_text()
    assert "d" * 64 in text and "e" * 64 in text
    assert len([ln for ln in text.splitlines() if ln]) == 2


class _FakeTransparencyLog:
    """In-memory Rekor-style log (stands in for the network transport)."""

    def __init__(self) -> None:
        self._entries: dict[str, dict[str, Any]] = {}

    def submit_entry(self, checkpoint: dict[str, Any], signature: bytes) -> str:
        index = str(len(self._entries))
        self._entries[index] = checkpoint
        return index  # inclusion proof (log index)

    def verify_entry(self, checkpoint: dict[str, Any], proof: str) -> bool:
        return self._entries.get(proof, {}).get("head_hash") == checkpoint.get("head_hash")


def test_transparency_log_witness_submit_and_verify() -> None:
    witness = TransparencyLogWitness(transport=_FakeTransparencyLog())
    cp = _checkpoint("f" * 64)
    proof = witness.submit(cp, signature=b"\x05" * 64)
    assert witness.verify_inclusion(cp, proof) is True
    assert witness.verify_inclusion(_checkpoint("0" * 64), proof) is False


def test_both_implementations_conform_to_protocol(tmp_path: Path) -> None:
    offline: WitnessAnchor = AppendOnlyMediumWitness(tmp_path / "w.log")
    online: WitnessAnchor = TransparencyLogWitness(transport=_FakeTransparencyLog())
    assert isinstance(offline, WitnessAnchor)
    assert isinstance(online, WitnessAnchor)


def test_submit_rejects_symlinked_medium(tmp_path: Path) -> None:
    """A symlinked witness medium must not be followed (O_NOFOLLOW)."""
    real = tmp_path / "real.log"
    real.write_text("", encoding="utf-8")
    link = tmp_path / "anchor.log"
    link.symlink_to(real)
    witness = AppendOnlyMediumWitness(link)
    with pytest.raises(OSError):
        witness.submit(_checkpoint("a" * 64), signature=b"\x01" * 64)


# ---------------------------------------------------------------------------
# SPEC-053 #2c — startup witness-consistency verification (fail closed federal)
# ---------------------------------------------------------------------------


def test_verify_no_local_checkpoint_is_ok(tmp_path: Path) -> None:
    """A fresh deployment with nothing anchored yet must not fail closed."""
    witness = AppendOnlyMediumWitness(tmp_path / "anchor.log")
    verify_local_head_witnessed(None, witness, federal=True)  # no raise


def test_verify_witnessed_head_passes(tmp_path: Path) -> None:
    witness = AppendOnlyMediumWitness(tmp_path / "anchor.log")
    cp = _checkpoint("b" * 64)
    witness.submit(cp, signature=b"\x02" * 64)
    verify_local_head_witnessed(cp, witness, federal=True)  # no raise


def test_verify_divergence_fails_closed_at_federal(tmp_path: Path) -> None:
    """A local head absent from the external witness (rollback + re-anchor) is a
    divergence — at federal it must fail closed, not warn."""
    witness = AppendOnlyMediumWitness(tmp_path / "anchor.log")
    witness.submit(_checkpoint("honest" + "0" * 58), signature=b"\x03" * 64)
    rolled_back = _checkpoint("rollbk" + "0" * 58)  # never witnessed
    with pytest.raises(WitnessDivergenceError):
        verify_local_head_witnessed(rolled_back, witness, federal=True)


def test_verify_missing_witness_medium_fails_closed_at_federal(tmp_path: Path) -> None:
    """A missing/unavailable witness at federal with a live local head fails closed."""
    witness = AppendOnlyMediumWitness(tmp_path / "does-not-exist.log")
    with pytest.raises(WitnessDivergenceError):
        verify_local_head_witnessed(_checkpoint("c" * 64), witness, federal=True)


def test_verify_divergence_warns_only_below_federal(tmp_path: Path) -> None:
    """Non-federal tiers warn on divergence but do not fail closed."""
    witness = AppendOnlyMediumWitness(tmp_path / "anchor.log")
    witness.submit(_checkpoint("d" * 64), signature=b"\x04" * 64)
    # No raise even though the head was never witnessed.
    verify_local_head_witnessed(_checkpoint("e" * 64), witness, federal=False)
