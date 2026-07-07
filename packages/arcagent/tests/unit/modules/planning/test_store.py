"""Unit tests for durable plan state (SPEC-040 T-020..T-022)."""

from __future__ import annotations

from pathlib import Path

import pytest
from arctrust import AuditEvent, WormSink, generate_keypair
from arctrust.signer import InProcessSigner

from arcagent.modules.planning.models import (
    Plan,
    PlanBudget,
    PlanStatus,
    PlanStep,
    StepStatus,
)
from arcagent.modules.planning.store import PlanIntegrityError, PlanStore


def _plan(plan_id: str = "plan_1", status: PlanStatus = PlanStatus.ACTIVE) -> Plan:
    return Plan(
        plan_id=plan_id,
        goal="g",
        goal_source_did="did:arc:user",
        parent_goal_hash="hash",
        status=status,
        steps=[PlanStep(step_id="a", description="do a")],
        max_replans=3,
        budget=PlanBudget(max_tokens=100),
    )


class _CapturingSink:
    """AuditSink that records every event for assertions."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class TestSaveLoad:
    def test_round_trip_equal(self, tmp_path: Path) -> None:
        store = PlanStore(tmp_path / "plans")
        plan = _plan()
        store.save(plan, action="plan.created")
        assert store.load("plan_1") == plan

    def test_save_is_atomic_no_tmp_left(self, tmp_path: Path) -> None:
        store = PlanStore(tmp_path / "plans")
        store.save(_plan(), action="plan.created")
        leftovers = list((tmp_path / "plans").glob("*.tmp"))
        assert leftovers == []

    def test_load_missing_raises(self, tmp_path: Path) -> None:
        store = PlanStore(tmp_path / "plans")
        with pytest.raises(FileNotFoundError):
            store.load("ghost")

    def test_traversal_key_rejected(self, tmp_path: Path) -> None:
        store = PlanStore(tmp_path / "plans")
        bad = _plan(plan_id="../escape")
        with pytest.raises(ValueError, match="invalid|escape"):
            store.save(bad, action="plan.created")


class TestActivePlanAndIntegrity:
    def test_active_plan_returns_active(self, tmp_path: Path) -> None:
        store = PlanStore(tmp_path / "plans")
        store.save(_plan("done", PlanStatus.COMPLETED), action="plan.completed")
        store.save(_plan("live", PlanStatus.ACTIVE), action="plan.created")
        active = store.active_plan()
        assert active is not None
        assert active.plan_id == "live"

    def test_no_active_plan_returns_none(self, tmp_path: Path) -> None:
        store = PlanStore(tmp_path / "plans")
        store.save(_plan("done", PlanStatus.COMPLETED), action="plan.completed")
        assert store.active_plan() is None

    def test_corrupt_file_rejected(self, tmp_path: Path) -> None:
        store = PlanStore(tmp_path / "plans")
        store.save(_plan(), action="plan.created")
        (tmp_path / "plans" / "plan_1.json").write_text("{ truncated", encoding="utf-8")
        with pytest.raises(PlanIntegrityError):
            store.load("plan_1")


class TestAudit:
    def test_every_save_emits_event(self, tmp_path: Path) -> None:
        sink = _CapturingSink()
        store = PlanStore(tmp_path / "plans", audit_sink=sink, actor_did="did:arc:agent")
        plan = _plan()
        store.save(plan, action="plan.created")
        plan.get_step("a").status = StepStatus.SUCCEEDED
        store.save(plan, action="plan.step.succeeded", target="a")
        actions = [e.action for e in sink.events]
        assert actions == ["plan.created", "plan.step.succeeded"]
        assert sink.events[0].actor_did == "did:arc:agent"
        assert sink.events[0].target == "plan_1"
        assert sink.events[1].target == "a"

    def test_worm_chain_verifies(self, tmp_path: Path) -> None:
        kp = generate_keypair()
        sink = WormSink(tmp_path / "audit.jsonl", InProcessSigner(kp.private_key))
        store = PlanStore(tmp_path / "plans", audit_sink=sink, actor_did="did:arc:agent")
        plan = _plan()
        store.save(plan, action="plan.created")
        store.save(plan, action="plan.completed")
        assert sink.verify_chain()
        sink.close()


class TestPlanFileSignature:
    """F4: an operator-signed plan file fails closed when tampered."""

    @staticmethod
    def _signed_store(tmp_path: Path) -> tuple[PlanStore, Path]:
        # Nest the workspace so the operator-owned sidecar (<agent_root>/.audit)
        # lands inside the unique tmp_path — mirrors ws=workspace, agent_root=ws.parent.
        kp = generate_keypair()
        plans_dir = tmp_path / "ws" / "plans"
        store = PlanStore(
            plans_dir,
            operator_signer=InProcessSigner(kp.private_key),
            actor_did="did:arc:agent",
        )
        return store, plans_dir

    def test_signed_plan_round_trips(self, tmp_path: Path) -> None:
        store, _ = self._signed_store(tmp_path)
        plan = _plan()
        store.save(plan, action="plan.created")
        assert store.load("plan_1") == plan  # signature verifies

    def test_tampered_plan_file_fails_closed(self, tmp_path: Path) -> None:
        store, plans_dir = self._signed_store(tmp_path)
        plan = _plan()
        store.save(plan, action="plan.created")
        # Agent with workspace write flips a FAILED step to SUCCEEDED on disk.
        forged = plan.model_copy(deep=True)
        forged.get_step("a").status = StepStatus.SUCCEEDED
        (plans_dir / "plan_1.json").write_text(
            forged.model_dump_json(indent=2), encoding="utf-8"
        )
        with pytest.raises(PlanIntegrityError, match="tampered"):
            store.load("plan_1")

    def test_unsigned_plan_file_fails_closed(self, tmp_path: Path) -> None:
        store, plans_dir = self._signed_store(tmp_path)
        # A plan file injected without ever passing through the operator signer.
        (plans_dir).mkdir(parents=True, exist_ok=True)
        (plans_dir / "inject.json").write_text(
            _plan("inject").model_dump_json(indent=2), encoding="utf-8"
        )
        with pytest.raises(PlanIntegrityError, match="no operator signature"):
            store.load("inject")

    def test_tampered_active_plan_not_resumed(self, tmp_path: Path) -> None:
        store, plans_dir = self._signed_store(tmp_path)
        plan = _plan()
        store.save(plan, action="plan.created")
        forged = plan.model_copy(deep=True)
        forged.get_step("a").status = StepStatus.SUCCEEDED
        (plans_dir / "plan_1.json").write_text(
            forged.model_dump_json(indent=2), encoding="utf-8"
        )
        # active_plan() skips the tampered file rather than resuming a forgery.
        assert store.active_plan() is None
