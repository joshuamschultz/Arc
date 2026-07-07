"""Cross-package integration test: arcllm trace-checkpoint signed anchor.

Proves the full loop end-to-end across arcllm (capture) and arctrust
(signing/anchoring):

1. arcllm's ``JSONLTraceStore`` builds a real pre-purge checkpoint and hands
   it to a ``checkpoint_sink`` callback.
2. The caller (this test, standing in for a real wiring layer) signs and
   durably anchors that checkpoint in an arctrust ``WormSink`` as an
   ordinary ``AuditEvent`` with ``action="trace.checkpoint"``.
3. HONEST PATH — after a legitimate retention purge, the anchored head is
   still present in the live store: ``read_verified_anchor`` +
   ``verify_against_anchor`` both hold.
4. DETECTION PATH — a malicious rollback that removes the anchored head is
   caught by ``verify_against_anchor``, even though arcllm's own
   ``verify_chain()`` still passes over the (now-truncated) records that
   remain — that blind spot is exactly the gap this feature closes.

This is a dev-only, test-time cross-import: arcllm's runtime source never
imports arctrust (CLAUDE.md "don't mix concerns" — see ``trace_store.py``
and ``trace_retention.py``). Only this test imports both, and only because
both packages are installed in the same uv workspace venv.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from arctrust.audit import AuditEvent, WormSink, emit, read_verified_anchor
from arctrust.keypair import generate_keypair
from arctrust.signer import InProcessSigner

from arcllm.trace_retention import verify_against_anchor
from arcllm.trace_store import JSONLTraceStore, TraceRecord


def _write_rotated_file(traces_dir: Path, date_str: str) -> Path:
    """Write a minimal, already-rotated trace file for a past date."""
    traces_dir.mkdir(parents=True, exist_ok=True)
    path = traces_dir / f"traces-{date_str}.jsonl"
    rec = TraceRecord(
        trace_id=f"old-{date_str}",
        timestamp=f"{date_str}T00:00:00+00:00",
        provider="anthropic",
        model="claude",
    ).with_hash("0" * 64)
    path.write_text(json.dumps(rec.model_dump()) + "\n")
    return path


class TestTraceCheckpointSignedAnchorIntegration:
    async def test_honest_purge_survives_anchor_and_malicious_rollback_is_detected(
        self, tmp_path: Path
    ) -> None:
        chain_path = tmp_path / "audit" / "chain.jsonl"
        kp = generate_keypair()
        sink = WormSink(chain_path, InProcessSigner(kp.private_key))

        def _anchor(checkpoint: dict[str, object]) -> None:
            emit(
                AuditEvent(
                    actor_did="did:arc:test:exec/00000001",
                    action="trace.checkpoint",
                    target="traces",
                    outcome="allow",
                    extra=checkpoint,
                ),
                sink,
            )

        agent_root = tmp_path / "agent"
        traces_dir = agent_root / "traces"
        old_date = (datetime.now(UTC) - timedelta(days=10)).strftime("%Y-%m-%d")
        old_file = _write_rotated_file(traces_dir, old_date)

        store = JSONLTraceStore(agent_root, retention_max_age_days=5, checkpoint_sink=_anchor)
        for i in range(3):
            await store.append(
                TraceRecord(provider="anthropic", model="claude", trace_id=f"live-{i}")
            )

        # Rotation boundary: anchors the pre-purge checkpoint to the WORM
        # chain, then purges the old file (age 10 > max_age_days=5).
        await store._maybe_purge()

        assert not old_file.exists()

        anchor = read_verified_anchor(chain_path, kp.public_key)
        assert anchor is not None

        # --- HONEST PATH ---
        # The legitimate purge already ran; the anchored head (captured
        # from a live record before deletion) still survives it.
        assert verify_against_anchor(traces_dir, anchor) is True
        # arcllm's own internal chain check also still passes.
        assert await store.verify_chain() is True

        # --- DETECTION PATH ---
        # Malicious rollback: truncate the live store back past the last
        # anchored head by deleting the very file that carries it.
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        live_file = traces_dir / f"traces-{today}.jsonl"
        live_file.unlink()

        # arcllm's own hash-chain check is blind to this: with no records
        # left on disk, internal consistency is vacuously satisfied.
        assert await store.verify_chain() is True

        # But the cross-package anchor check catches it: the anchored
        # head_hash is no longer present anywhere in the live store.
        assert verify_against_anchor(traces_dir, anchor) is False

        # The WORM chain itself is untouched by the arcllm-side rollback —
        # arctrust's half of the attestation remains fully verifiable.
        assert read_verified_anchor(chain_path, kp.public_key) is not None
