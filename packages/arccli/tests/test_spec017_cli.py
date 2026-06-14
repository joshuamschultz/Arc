"""SPEC-017 Phase 8 — policy / completion / schedule helpers.

Direct-call tests against the plain Python helpers in
``arccli.commands.spec017``. The original Click-group wrappers were removed
(architecture rule: no click in arccli); behavior is verified at the
function boundary.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arccli.commands.spec017 import (
    completion_history,
    policy_evaluate,
    policy_layers,
    schedule_list,
    schedule_migrate,
)


class TestPolicyLayers:
    def test_federal_lists_all_five(self) -> None:
        payload = policy_layers(tier="federal")
        assert payload["tier"] == "federal"
        assert payload["layers"] == [
            "identity",
            "global",
            "provider",
            "agent",
            "team",
            "sandbox",
        ]

    def test_enterprise_drops_team(self) -> None:
        payload = policy_layers(tier="enterprise")
        assert "team" not in payload["layers"]

    def test_personal_has_identity_and_global(self) -> None:
        payload = policy_layers(tier="personal")
        assert payload["layers"] == ["identity", "global"]


class TestPolicyEvaluate:
    def test_allow_decision(self) -> None:
        payload = policy_evaluate(tool_name="read", tier="personal")
        assert payload["outcome"] == "allow"


class TestCompletionHistory:
    def test_empty_workspace_returns_empty_list(self, tmp_path: Path) -> None:
        payload = completion_history(path=str(tmp_path))
        assert payload == {"events": []}

    def test_reads_audit_log(self, tmp_path: Path) -> None:
        audit_dir = tmp_path / "workspace" / "audit"
        audit_dir.mkdir(parents=True)
        log = audit_dir / "2026-04-18.jsonl"
        entries = [
            {"event_type": "other", "data": {}},
            {"event_type": "loop.completed", "status": "success"},
            {"event_type": "loop.completed", "status": "failed"},
        ]
        log.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")

        payload = completion_history(path=str(tmp_path), limit=10)
        assert len(payload["events"]) == 2


class TestScheduleList:
    def test_missing_state_file_returns_empty(self, tmp_path: Path) -> None:
        payload = schedule_list(path=str(tmp_path))
        assert payload == {"schedules": []}

    def test_reads_persisted_state(self, tmp_path: Path) -> None:
        proactive_dir = tmp_path / "workspace" / "proactive"
        proactive_dir.mkdir(parents=True)
        expected = {"schedules": [{"id": "nightly", "interval_seconds": 3600}]}
        (proactive_dir / "schedules.json").write_text(json.dumps(expected), encoding="utf-8")

        payload = schedule_list(path=str(tmp_path))
        assert payload == expected


class TestScheduleMigrate:
    def test_noop_when_legacy_dir_absent(self, tmp_path: Path) -> None:
        payload = schedule_migrate(path=str(tmp_path))
        assert payload["status"] == "no-op"

    def test_migrates_legacy_schedules(self, tmp_path: Path) -> None:
        legacy = tmp_path / "workspace" / "scheduler"
        legacy.mkdir(parents=True)
        (legacy / "state.jsonl").write_text(
            json.dumps(
                {
                    "id": "nightly-ingest",
                    "interval_seconds": 3600,
                    "kind": "cron",
                    "cron_expression": "0 2 * * *",
                }
            )
            + "\n"
            + json.dumps(
                {
                    "id": "hourly-ping",
                    "interval_seconds": 60,
                    "kind": "cron",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        payload = schedule_migrate(path=str(tmp_path))
        assert payload["status"] == "migrated"
        assert payload["count"] == 2

        target = tmp_path / "workspace" / "proactive" / "schedules.json"
        assert target.exists()
        migrated = json.loads(target.read_text())
        ids = {s["id"] for s in migrated["schedules"]}
        assert ids == {"nightly-ingest", "hourly-ping"}
        nightly = next(s for s in migrated["schedules"] if s["id"] == "nightly-ingest")
        assert nightly["metadata"]["original_cron"] == "0 2 * * *"

    def test_dry_run_does_not_write(self, tmp_path: Path) -> None:
        legacy = tmp_path / "workspace" / "scheduler"
        legacy.mkdir(parents=True)
        (legacy / "state.jsonl").write_text(
            json.dumps({"id": "x", "interval_seconds": 60}) + "\n",
            encoding="utf-8",
        )

        payload = schedule_migrate(path=str(tmp_path), dry_run=True)
        assert payload["status"] == "dry-run"
        assert payload["count"] == 1

        target = tmp_path / "workspace" / "proactive" / "schedules.json"
        assert not target.exists()


_ = pytest
