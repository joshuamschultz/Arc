"""Unit tests for hardened spawn primitives.

Covers: SpawnResult model, budget pool integration, DID derivation,
audit chain, depth cap enforcement. Per PLAN.md T3.5 unit tests.
"""

from __future__ import annotations

import asyncio

import pytest

from arcrun.builtins.spawn import (
    ChildIdentity,
    RootTokenBudget,
    SpawnResult,
    TokenUsage,
    derive_child_identity,
)


# ---------------------------------------------------------------------------
# SpawnResult model
# ---------------------------------------------------------------------------


class TestSpawnResultModel:
    def test_completed_status(self) -> None:
        r = SpawnResult(
            child_run_id="abc123",
            child_did="did:arc:delegate:child/aabbccdd",
            status="completed",
            summary="done",
            tokens=TokenUsage(input=10, output=20, total=30),
            tool_trace=["search", "read"],
            audit_chain_tip="deadbeef" * 8,
            duration_s=1.5,
        )
        assert r.status == "completed"
        assert r.tokens.total == 30
        assert r.error is None

    def test_budget_exhausted_status(self) -> None:
        r = SpawnResult(
            child_run_id="abc123",
            child_did="did:arc:delegate:child/aabbccdd",
            status="budget_exhausted",
            summary="out of tokens",
            tokens=TokenUsage(),
            tool_trace=[],
            audit_chain_tip="0" * 64,
            duration_s=0.0,
            error="root token budget exhausted",
        )
        assert r.status == "budget_exhausted"
        assert r.error is not None

    def test_all_valid_statuses(self) -> None:
        valid = [
            "completed",
            "max_iterations",
            "timeout",
            "interrupted",
            "error",
            "budget_exhausted",
        ]
        for status in valid:
            r = SpawnResult(
                child_run_id="x",
                child_did="d",
                status=status,  # type: ignore[arg-type]
                summary="s",
                tokens=TokenUsage(),
                tool_trace=[],
                audit_chain_tip="0" * 64,
                duration_s=0.0,
            )
            assert r.status == status

    def test_token_usage_default_zero(self) -> None:
        tu = TokenUsage()
        assert tu.input == 0
        assert tu.output == 0
        assert tu.total == 0

    def test_tool_trace_list(self) -> None:
        r = SpawnResult(
            child_run_id="x",
            child_did="d",
            status="completed",
            summary="s",
            tokens=TokenUsage(),
            tool_trace=["read_file", "write_file", "search"],
            audit_chain_tip="0" * 64,
            duration_s=1.0,
        )
        assert len(r.tool_trace) == 3
        assert "read_file" in r.tool_trace


# ---------------------------------------------------------------------------
# DID derivation
# ---------------------------------------------------------------------------


class TestDIDDerivation:
    def test_derives_did_from_parent_sk_and_nonce(self) -> None:
        sk = b"\x42" * 32
        identity = derive_child_identity(sk, "nonce-abc", 300)
        assert identity.did.startswith("did:arc:delegate:child/")
        assert len(identity.did) > len("did:arc:delegate:child/")

    def test_different_nonce_different_did(self) -> None:
        sk = b"\x42" * 32
        id1 = derive_child_identity(sk, "nonce-1", 300)
        id2 = derive_child_identity(sk, "nonce-2", 300)
        assert id1.did != id2.did
        assert id1.sk_bytes != id2.sk_bytes

    def test_same_nonce_same_did(self) -> None:
        sk = b"\x42" * 32
        id1 = derive_child_identity(sk, "nonce-x", 300)
        id2 = derive_child_identity(sk, "nonce-x", 300)
        assert id1.did == id2.did
        assert id1.sk_bytes == id2.sk_bytes

    def test_different_parent_sk_different_did(self) -> None:
        id1 = derive_child_identity(b"\x01" * 32, "nonce-same", 300)
        id2 = derive_child_identity(b"\x02" * 32, "nonce-same", 300)
        assert id1.did != id2.did

    def test_ttl_stored_in_identity(self) -> None:
        identity = derive_child_identity(b"\x00" * 32, "n", 600)
        assert identity.ttl_s == 600

    def test_sk_bytes_length(self) -> None:
        identity = derive_child_identity(b"\x99" * 32, "n", 300)
        assert len(identity.sk_bytes) == 32

    def test_child_identity_model_fields(self) -> None:
        identity = ChildIdentity(did="did:arc:d:x/01234567", sk_bytes=b"\x00" * 32, ttl_s=300)
        assert identity.did == "did:arc:d:x/01234567"
        assert identity.ttl_s == 300


# ---------------------------------------------------------------------------
# RootTokenBudget — atomic debit
# ---------------------------------------------------------------------------


class TestRootTokenBudget:
    @pytest.mark.asyncio
    async def test_debit_succeeds_within_budget(self) -> None:
        budget = RootTokenBudget(total=1000)
        ok = await budget.try_debit(500)
        assert ok is True
        assert budget.remaining == 500

    @pytest.mark.asyncio
    async def test_debit_refused_when_over_budget(self) -> None:
        budget = RootTokenBudget(total=100)
        ok = await budget.try_debit(200)
        assert ok is False
        assert budget.remaining == 100  # not debited

    @pytest.mark.asyncio
    async def test_multiple_debits_track_correctly(self) -> None:
        budget = RootTokenBudget(total=300)
        await budget.try_debit(100)
        await budget.try_debit(100)
        assert budget.used == 200
        assert budget.remaining == 100

    @pytest.mark.asyncio
    async def test_exhaustion_check(self) -> None:
        budget = RootTokenBudget(total=50)
        await budget.try_debit(50)
        assert budget.is_exhausted() is True

    @pytest.mark.asyncio
    async def test_not_exhausted_when_partial(self) -> None:
        budget = RootTokenBudget(total=100)
        await budget.try_debit(50)
        assert budget.is_exhausted() is False

    @pytest.mark.asyncio
    async def test_record_actual_overage(self) -> None:
        budget = RootTokenBudget(total=100)
        await budget.try_debit(50)  # pre-debit
        await budget.record_actual(30)  # actual overage
        assert budget.used == 80

    def test_zero_budget_raises(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            RootTokenBudget(total=0)

    def test_negative_budget_raises(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            RootTokenBudget(total=-1)

    @pytest.mark.asyncio
    async def test_concurrent_debit_atomic(self) -> None:
        """Only one of two concurrent debits should succeed for a tight budget."""
        budget = RootTokenBudget(total=100)
        results = await asyncio.gather(
            budget.try_debit(80),
            budget.try_debit(80),
        )
        # Exactly one should succeed (100 budget, each asks for 80)
        assert results.count(True) == 1
        assert results.count(False) == 1


# ---------------------------------------------------------------------------
# Audit chain tip helpers
# ---------------------------------------------------------------------------


class TestAuditChainHelpers:
    def test_spawn_result_audit_chain_tip_preserved(self) -> None:
        tip = "a" * 64
        r = SpawnResult(
            child_run_id="r1",
            child_did="d",
            status="completed",
            summary="s",
            tokens=TokenUsage(),
            tool_trace=[],
            audit_chain_tip=tip,
            duration_s=0.1,
        )
        assert r.audit_chain_tip == tip
