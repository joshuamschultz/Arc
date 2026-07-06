"""SPEC-053 T-05 — federal witness catches a rollback a holder of the operator
key could otherwise hide.

Lands in arcagent (not arctrust) because it needs BOTH arctrust (operator key,
witness, ``read_verified_anchor``) and ``arcllm.trace_retention`` — keeping the
arctrust import boundary clean (PLAN T-05 note).

Threat: an attacker with the OPERATOR key rolls back the live trace store and
re-anchors the rolled-back head in the local WORM chain. The local chain alone
is fooled. The EXTERNAL witness still holds the original head, and the live
store no longer contains it — so the rollback is detected.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from arcllm.trace_retention import build_checkpoint, verify_against_anchor
from arctrust import AppendOnlyMediumWitness, OperatorKey, read_verified_anchor

from arcagent.core.model_manager import build_checkpoint_sink


def _write_traces(traces_dir: Path, record_hashes: list[str]) -> None:
    traces_dir.mkdir(parents=True, exist_ok=True)
    path = traces_dir / "traces-2026-07-06.jsonl"
    lines = [json.dumps({"record_hash": h, "event_type": "llm_call"}) for h in record_hashes]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_rollback_past_witnessed_anchor_is_detected(tmp_path: Path) -> None:
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    traces_dir = agent_root / "traces"
    operator = OperatorKey.generate()
    witness = AppendOnlyMediumWitness(agent_root / "witness" / "anchor.log")
    chain = agent_root / ".audit" / "trace-checkpoint.worm"

    # 1. Real trace history; the honest head is "bbb".
    _write_traces(traces_dir, ["aaa", "bbb"])
    honest_cp: dict[str, Any] = build_checkpoint(traces_dir)
    assert honest_cp["head_hash"] == "bbb"

    # 2. Operator-anchors the honest head in the WORM chain + external witness.
    sink = build_checkpoint_sink(agent_root, operator, actor_did="did:arc:test:exec/aa")
    sink(honest_cp)
    anchored = read_verified_anchor(chain, operator.public_key)
    assert anchored is not None and anchored["head_hash"] == "bbb"
    proof = witness.submit(honest_cp, signature=b"\x00" * 64)

    # 3. Attacker (holds the operator key) rolls the store back past "bbb" and
    #    re-anchors the rolled-back head "aaa" in the LOCAL chain.
    _write_traces(traces_dir, ["aaa"])
    rolled_back_cp = build_checkpoint(traces_dir)
    assert rolled_back_cp["head_hash"] == "aaa"
    sink(rolled_back_cp)  # append a fresh operator-signed anchor for "aaa"

    # 4a. The LOCAL chain alone is fooled: its latest verified anchor now says
    #     "aaa", which the rolled-back store still contains.
    local_latest = read_verified_anchor(chain, operator.public_key)
    assert local_latest is not None and local_latest["head_hash"] == "aaa"
    assert verify_against_anchor(traces_dir, local_latest) is True

    # 4b. The EXTERNAL witness still holds the honest head "bbb" — which the
    #     rolled-back store no longer contains. The forgery is CAUGHT.
    assert witness.verify_inclusion(honest_cp, proof) is True
    assert verify_against_anchor(traces_dir, honest_cp) is False
