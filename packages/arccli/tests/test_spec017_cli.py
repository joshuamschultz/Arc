"""SPEC-017 Phase 8 — CLI mirror for policy / completion / schedule.

Commands are thin wrappers around core APIs, invoked via Click's
``CliRunner`` so we don't actually shell out.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from arccli.commands.spec017 import (
    completion_group,
    policy_group,
    schedule_group,
)


class TestPolicyLayers:
    def test_federal_lists_all_five(self) -> None:
        runner = CliRunner()
        result = runner.invoke(policy_group, ["layers", "--tier", "federal"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["tier"] == "federal"
        assert payload["layers"] == [
            "global",
            "provider",
            "agent",
            "team",
            "sandbox",
        ]

    def test_enterprise_drops_team(self) -> None:
        runner = CliRunner()
        result = runner.invoke(policy_group, ["layers", "--tier", "enterprise"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert "team" not in payload["layers"]

    def test_personal_single_layer(self) -> None:
        runner = CliRunner()
        result = runner.invoke(policy_group, ["layers", "--tier", "personal"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["layers"] == ["global"]


class TestPolicyEvaluate:
    def test_allow_decision(self) -> None:
        runner = CliRunner()
        result = runner.invoke(
            policy_group,
            ["evaluate", "--tier", "personal", "--tool", "read"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["outcome"] == "allow"


class TestCompletionHistory:
    def test_empty_workspace_returns_empty_list(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            completion_group,
            ["history", "--path", str(tmp_path)],
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
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

        runner = CliRunner()
        result = runner.invoke(
            completion_group,
            ["history", "--path", str(tmp_path), "--limit", "10"],
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert len(payload["events"]) == 2


class TestScheduleList:
    def test_missing_state_file_returns_empty(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            schedule_group,
            ["list", "--path", str(tmp_path)],
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload == {"schedules": []}

    def test_reads_persisted_state(self, tmp_path: Path) -> None:
        proactive_dir = tmp_path / "workspace" / "proactive"
        proactive_dir.mkdir(parents=True)
        payload = {"schedules": [{"id": "nightly", "interval_seconds": 3600}]}
        (proactive_dir / "schedules.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

        runner = CliRunner()
        result = runner.invoke(
            schedule_group,
            ["list", "--path", str(tmp_path)],
        )
        assert result.exit_code == 0
        assert json.loads(result.output) == payload


class TestScheduleMigrate:
    def test_noop_when_legacy_dir_absent(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            schedule_group, ["migrate", "--path", str(tmp_path)]
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
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

        runner = CliRunner()
        result = runner.invoke(
            schedule_group, ["migrate", "--path", str(tmp_path)]
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["status"] == "migrated"
        assert payload["count"] == 2

        target = tmp_path / "workspace" / "proactive" / "schedules.json"
        assert target.exists()
        migrated = json.loads(target.read_text())
        ids = {s["id"] for s in migrated["schedules"]}
        assert ids == {"nightly-ingest", "hourly-ping"}
        # Metadata preserves cron expression
        nightly = next(s for s in migrated["schedules"] if s["id"] == "nightly-ingest")
        assert nightly["metadata"]["original_cron"] == "0 2 * * *"

    def test_dry_run_does_not_write(self, tmp_path: Path) -> None:
        legacy = tmp_path / "workspace" / "scheduler"
        legacy.mkdir(parents=True)
        (legacy / "state.jsonl").write_text(
            json.dumps({"id": "x", "interval_seconds": 60}) + "\n",
            encoding="utf-8",
        )

        runner = CliRunner()
        result = runner.invoke(
            schedule_group,
            ["migrate", "--path", str(tmp_path), "--dry-run"],
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["status"] == "dry-run"
        assert payload["count"] == 1
        # Target file NOT written
        target = tmp_path / "workspace" / "proactive" / "schedules.json"
        assert not target.exists()


# CLI commands are synchronous Click entry points — no asyncio needed.
_ = pytest
