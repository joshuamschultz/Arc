"""Tests for ConfigController — get/set, on_change, audit events."""

import pytest
from pydantic import ValidationError

from arcllm.config_controller import ConfigController, ConfigSnapshot
from arcllm.exceptions import ArcLLMConfigError
from arcllm.trace_store import TraceRecord


class TestConfigSnapshot:
    def test_frozen(self):
        snap = ConfigSnapshot(model="claude-sonnet-4")
        with pytest.raises(ValidationError):
            snap.model = "gpt-4"  # type: ignore[misc]

    def test_defaults(self):
        snap = ConfigSnapshot(model="claude-sonnet-4")
        assert snap.temperature == 0.7
        assert snap.max_tokens == 4096
        assert snap.daily_budget_limit is None
        assert snap.failover_chain == []

    def test_custom_values(self):
        snap = ConfigSnapshot(
            model="gpt-4o",
            temperature=0.5,
            max_tokens=8192,
            daily_budget_limit=10.0,
            monthly_budget_limit=100.0,
            failover_chain=["gpt-4o-mini", "claude-haiku"],
        )
        assert snap.model == "gpt-4o"
        assert snap.temperature == 0.5
        assert snap.failover_chain == ["gpt-4o-mini", "claude-haiku"]


class TestConfigController:
    def test_get_snapshot_returns_initial(self):
        ctrl = ConfigController({"model": "claude-sonnet-4"})
        snap = ctrl.get_snapshot()
        assert snap.model == "claude-sonnet-4"
        assert snap.temperature == 0.7

    def test_patch_updates_snapshot(self):
        ctrl = ConfigController({"model": "claude-sonnet-4"})
        new = ctrl.patch({"temperature": 0.3}, actor="operator")
        assert new.temperature == 0.3
        assert ctrl.get_snapshot().temperature == 0.3

    def test_patch_preserves_unchanged_fields(self):
        ctrl = ConfigController(
            {"model": "claude-sonnet-4", "max_tokens": 8192}
        )
        new = ctrl.patch({"temperature": 0.5}, actor="test")
        assert new.max_tokens == 8192
        assert new.model == "claude-sonnet-4"

    def test_patch_multiple_fields(self):
        ctrl = ConfigController({"model": "claude-sonnet-4"})
        new = ctrl.patch(
            {"model": "gpt-4o", "temperature": 0.1, "max_tokens": 2048},
            actor="test",
        )
        assert new.model == "gpt-4o"
        assert new.temperature == 0.1
        assert new.max_tokens == 2048

    def test_patch_invalid_key_rejected(self):
        ctrl = ConfigController({"model": "claude-sonnet-4"})
        with pytest.raises(ArcLLMConfigError, match="Cannot patch"):
            ctrl.patch({"bogus_field": "value"}, actor="test")

    def test_patch_empty_updates_rejected(self):
        ctrl = ConfigController({"model": "claude-sonnet-4"})
        with pytest.raises(ArcLLMConfigError, match="at least one update"):
            ctrl.patch({}, actor="test")

    def test_patch_no_op_returns_same(self):
        ctrl = ConfigController({"model": "claude-sonnet-4", "temperature": 0.7})
        old = ctrl.get_snapshot()
        new = ctrl.patch({"temperature": 0.7}, actor="test")
        assert new is old

    def test_on_change_fires_after_patch(self):
        snapshots: list[ConfigSnapshot] = []
        ctrl = ConfigController({"model": "claude-sonnet-4"})
        ctrl.on_change(snapshots.append)

        ctrl.patch({"temperature": 0.5}, actor="test")

        assert len(snapshots) == 1
        assert snapshots[0].temperature == 0.5

    def test_on_change_not_fired_for_no_op(self):
        snapshots: list[ConfigSnapshot] = []
        ctrl = ConfigController({"model": "claude-sonnet-4", "temperature": 0.7})
        ctrl.on_change(snapshots.append)

        ctrl.patch({"temperature": 0.7}, actor="test")

        assert len(snapshots) == 0

    def test_multiple_on_change_callbacks(self):
        calls_a: list[ConfigSnapshot] = []
        calls_b: list[ConfigSnapshot] = []
        ctrl = ConfigController({"model": "claude-sonnet-4"})
        ctrl.on_change(calls_a.append)
        ctrl.on_change(calls_b.append)

        ctrl.patch({"temperature": 0.3}, actor="test")

        assert len(calls_a) == 1
        assert len(calls_b) == 1


class TestConfigControllerAuditEvents:
    """Task 2.5: ConfigController emits audit TraceRecords."""

    def test_patch_emits_config_change_event(self):
        events: list[TraceRecord] = []
        ctrl = ConfigController(
            {"model": "claude-sonnet-4"}, on_event=events.append
        )

        ctrl.patch({"temperature": 0.3}, actor="operator-1")

        assert len(events) == 1
        rec = events[0]
        assert rec.event_type == "config_change"
        assert rec.event_data is not None
        assert rec.event_data["actor"] == "operator-1"
        assert "temperature" in rec.event_data["changes"]
        change = rec.event_data["changes"]["temperature"]
        assert change["old"] == 0.7
        assert change["new"] == 0.3

    def test_no_event_on_no_op_patch(self):
        events: list[TraceRecord] = []
        ctrl = ConfigController(
            {"model": "claude-sonnet-4", "temperature": 0.7},
            on_event=events.append,
        )

        ctrl.patch({"temperature": 0.7}, actor="test")

        assert len(events) == 0

    def test_event_has_all_changes(self):
        events: list[TraceRecord] = []
        ctrl = ConfigController(
            {"model": "claude-sonnet-4", "temperature": 0.7},
            on_event=events.append,
        )

        ctrl.patch(
            {"model": "gpt-4o", "temperature": 0.1}, actor="admin"
        )

        assert len(events) == 1
        changes = events[0].event_data["changes"]
        assert "model" in changes
        assert "temperature" in changes
        assert changes["model"]["old"] == "claude-sonnet-4"
        assert changes["model"]["new"] == "gpt-4o"

    def test_no_event_when_on_event_none(self):
        ctrl = ConfigController({"model": "claude-sonnet-4"})
        # Should not raise
        ctrl.patch({"temperature": 0.5}, actor="test")
