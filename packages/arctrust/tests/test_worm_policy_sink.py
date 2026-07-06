"""SPEC-034 — tests for the audit->WORM policy adapter and emission invariants.

Covers REQ-016 (single emission), REQ-017 (WORM adapter + verifiable chain),
REQ-018 (fail-open on audit), REQ-019 (payload sufficiency, no raw args).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from arctrust.audit import AuditEvent, WormSink, verify_chain, worm_policy_sink
from arctrust.identity import AgentIdentity
from arctrust.keypair import generate_keypair
from arctrust.policy import (
    Decision,
    PolicyContext,
    PolicyPipeline,
    ToolCall,
    build_pipeline,
    sign_call,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_call(tool_name: str = "read_file", agent_did: str = "did:arc:t:e/aabb") -> ToolCall:
    return ToolCall(
        tool_name=tool_name,
        arguments={"secret_path": "/etc/shadow", "token": "s3cr3t"},
        agent_did=agent_did,
        session_id="sess-1",
        classification="UNCLASSIFIED",
    )


def make_ctx(tier: str = "personal") -> PolicyContext:
    return PolicyContext(tier=tier, policy_version="1.0", bundle_age_seconds=0.0)  # type: ignore[arg-type]


class AllowLayer:
    name = "allow_all"

    async def evaluate(self, call: ToolCall, ctx: PolicyContext) -> Decision:
        return Decision.allow(input_hash="abc", evaluated_at_us=int(time.monotonic() * 1_000_000))


class DenyLayer:
    name = "deny_all"

    async def evaluate(self, call: ToolCall, ctx: PolicyContext) -> Decision:
        return Decision.deny(
            layer=self.name,
            rule_id="deny_all.rule",
            reason="always deny",
            input_hash="abc",
            evaluated_at_us=int(time.monotonic() * 1_000_000),
        )


# ---------------------------------------------------------------------------
# REQ-017 — WORM adapter
# ---------------------------------------------------------------------------


class TestWormPolicySink:
    def test_maps_payload_to_audit_event(self) -> None:
        captured: list[AuditEvent] = []

        class Capture:
            def write(self, event: AuditEvent) -> None:
                captured.append(event)

        sink = worm_policy_sink(Capture())
        sink(
            "policy.evaluate",
            {
                "tool_name": "read_file",
                "agent_did": "did:arc:t:e/aabb",
                "classification": "SECRET",
                "tier": "federal",
                "decision": "deny",
                "rule_id": "provider.budget_exceeded",
                "layer": "provider",
                "reason": "over budget",
                "input_hash": "deadbeef",
                "arguments": {"raw": "should never appear"},
            },
        )
        assert len(captured) == 1
        event = captured[0]
        assert event.action == "policy.evaluate"
        assert event.outcome == "deny"
        assert event.target == "read_file"
        assert event.payload_hash == "deadbeef"
        assert event.classification == "SECRET"
        assert event.tier == "federal"
        assert event.extra["layer"] == "provider"
        assert event.extra["rule_id"] == "provider.budget_exceeded"
        # REQ-019: raw arguments never copied into the audit record.
        assert "arguments" not in event.extra
        assert "raw" not in str(event.extra)

    def test_writes_verifiable_worm_chain(self, tmp_path: Path) -> None:
        """REQ-017: adapter writes a WORM chain whose verify_chain() passes."""
        kp = generate_keypair()
        path = tmp_path / "policy-chain.jsonl"
        worm = WormSink(path, kp.private_key)
        sink = worm_policy_sink(worm)
        for outcome in ("allow", "deny", "allow"):
            sink(
                "policy.evaluate",
                {
                    "tool_name": "read_file",
                    "agent_did": "did:arc:t:e/aabb",
                    "classification": "UNCLASSIFIED",
                    "tier": "federal",
                    "decision": outcome,
                    "rule_id": None if outcome == "allow" else "x.rule",
                    "layer": None if outcome == "allow" else "provider",
                    "reason": None,
                    "input_hash": "h",
                },
            )
        worm.close()
        assert verify_chain(path, kp.public_key) is True


# ---------------------------------------------------------------------------
# REQ-016 — single emission point (lock existing behavior)
# ---------------------------------------------------------------------------


class TestSingleEmission:
    async def test_one_event_per_evaluate_mixed_outcomes(self) -> None:
        events: list[tuple[str, dict[str, Any]]] = []

        def sink(event_type: str, payload: dict[str, Any]) -> None:
            events.append((event_type, payload))

        pipeline = PolicyPipeline(layers=[AllowLayer()], audit_sink=sink)
        deny_pipeline = PolicyPipeline(layers=[DenyLayer()], audit_sink=sink)
        await pipeline.evaluate(make_call(tool_name="a"), make_ctx())
        await deny_pipeline.evaluate(make_call(tool_name="b"), make_ctx())
        await pipeline.evaluate(make_call(tool_name="c"), make_ctx())
        assert len(events) == 3
        outcomes = [p["decision"] for _, p in events]
        assert outcomes == ["allow", "deny", "allow"]


# ---------------------------------------------------------------------------
# REQ-018 — fail-open on audit
# ---------------------------------------------------------------------------


class TestFailOpenOnAudit:
    async def test_throwing_sink_does_not_break_or_alter_decision(self) -> None:
        def bad_sink(event_type: str, payload: dict[str, Any]) -> None:
            raise RuntimeError("sink down")

        allow_p = PolicyPipeline(layers=[AllowLayer()], audit_sink=bad_sink)
        deny_p = PolicyPipeline(layers=[DenyLayer()], audit_sink=bad_sink)
        assert (await allow_p.evaluate(make_call(), make_ctx())).outcome == "allow"
        assert (await deny_p.evaluate(make_call(), make_ctx())).outcome == "deny"

    async def test_throwing_worm_sink_via_adapter_is_swallowed(self, tmp_path: Path) -> None:
        class Broken:
            def write(self, event: AuditEvent) -> None:
                raise OSError("disk full")

        pipeline = PolicyPipeline(
            layers=[DenyLayer()], audit_sink=worm_policy_sink(Broken())
        )
        assert (await pipeline.evaluate(make_call(), make_ctx())).outcome == "deny"


# ---------------------------------------------------------------------------
# REQ-019 — payload sufficiency (keys present, no raw arguments)
# ---------------------------------------------------------------------------


class TestPayloadSufficiency:
    async def test_payload_has_reconstruction_keys_and_no_raw_args(self) -> None:
        events: list[dict[str, Any]] = []

        def sink(event_type: str, payload: dict[str, Any]) -> None:
            events.append(payload)

        pipeline = PolicyPipeline(layers=[DenyLayer()], audit_sink=sink)
        await pipeline.evaluate(make_call(), make_ctx("federal"))
        payload = events[0]
        for key in ("tier", "layer", "rule_id", "input_hash", "classification"):
            assert key in payload, f"missing {key}"
        assert "arguments" not in payload


# ---------------------------------------------------------------------------
# End-to-end: production-shaped pipeline -> WORM chain has a verifiable record
# ---------------------------------------------------------------------------


class TestPipelineToWorm:
    async def test_denied_call_produces_verifiable_policy_evaluate_record(
        self, tmp_path: Path
    ) -> None:
        kp = generate_keypair()
        path = tmp_path / "chain.jsonl"
        worm = WormSink(path, kp.private_key)
        ident = AgentIdentity.generate(org="test", agent_type="exec")
        pipeline = build_pipeline(
            tier="personal",
            global_deny_rules={"read_file": "denied for test"},
            audit_sink=worm_policy_sink(worm),
        )
        signed = sign_call(make_call(agent_did=ident.did), ident)
        decision = await pipeline.evaluate(signed, make_ctx("personal"))
        worm.close()
        assert decision.outcome == "deny"
        assert verify_chain(path, kp.public_key) is True
        # The chain contains a policy.evaluate record whose outcome is the decision.
        import json

        records = [json.loads(line) for line in path.read_text().splitlines() if line]
        actions = [r["event"]["action"] for r in records]
        assert "policy.evaluate" in actions
        pe = next(r for r in records if r["event"]["action"] == "policy.evaluate")
        assert pe["event"]["outcome"] == "deny"
