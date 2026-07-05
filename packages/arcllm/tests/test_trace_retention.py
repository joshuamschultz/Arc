"""Tests for arcllm.trace_retention — whole-file retention purge (SPEC-016)."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from arcllm.trace_retention import build_checkpoint, purge, verify_against_anchor
from arcllm.trace_store import JSONLTraceStore, TraceRecord


def _write_file(traces_dir: Path, date_str: str, n_records: int = 1) -> Path:
    """Write a minimal, valid-looking rotated trace file for a given date."""
    traces_dir.mkdir(parents=True, exist_ok=True)
    path = traces_dir / f"traces-{date_str}.jsonl"
    prev_hash = "0" * 64
    lines = []
    for i in range(n_records):
        rec = TraceRecord(
            trace_id=f"{date_str}-{i}",
            timestamp=f"{date_str}T00:00:0{i}+00:00",
            provider="anthropic",
            model="claude",
        ).with_hash(prev_hash)
        prev_hash = rec.record_hash
        lines.append(json.dumps(rec.model_dump()))
    path.write_text("\n".join(lines) + "\n")
    return path


def _days_ago(n: int) -> str:
    return (datetime.now(UTC) - timedelta(days=n)).strftime("%Y-%m-%d")


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


class TestPurgeByAge:
    async def test_deletes_files_older_than_max_age_days(self, tmp_path: Path):
        old_file = _write_file(tmp_path, _days_ago(10))
        recent_file = _write_file(tmp_path, _days_ago(1))

        deleted = await purge(tmp_path, max_age_days=5, max_bytes=None)

        assert old_file in deleted
        assert not old_file.exists()
        assert recent_file.exists()

    async def test_current_day_file_never_purged(self, tmp_path: Path):
        today_file = _write_file(tmp_path, _today())

        deleted = await purge(tmp_path, max_age_days=0, max_bytes=None)

        assert today_file.exists()
        assert today_file not in deleted

    async def test_no_bounds_configured_is_noop(self, tmp_path: Path):
        old_file = _write_file(tmp_path, _days_ago(100))
        deleted = await purge(tmp_path, max_age_days=None, max_bytes=None)
        assert deleted == []
        assert old_file.exists()


class TestPurgeBySize:
    async def test_deletes_oldest_first_until_under_max_bytes(self, tmp_path: Path):
        oldest = _write_file(tmp_path, _days_ago(3), n_records=20)
        middle = _write_file(tmp_path, _days_ago(2), n_records=20)
        newest = _write_file(tmp_path, _days_ago(1), n_records=20)

        total_before = sum(p.stat().st_size for p in (oldest, middle, newest))
        # Cap tight enough to force at least one deletion, loose enough to
        # keep the newest file.
        cap = total_before - oldest.stat().st_size - 1

        deleted = await purge(tmp_path, max_age_days=None, max_bytes=cap)

        assert oldest in deleted
        assert not oldest.exists()
        assert newest.exists()

    async def test_under_cap_deletes_nothing(self, tmp_path: Path):
        f1 = _write_file(tmp_path, _days_ago(3))
        f2 = _write_file(tmp_path, _days_ago(2))
        huge_cap = 10**9

        deleted = await purge(tmp_path, max_age_days=None, max_bytes=huge_cap)

        assert deleted == []
        assert f1.exists()
        assert f2.exists()


class TestPurgeSurvivorChainIntegrity:
    async def test_verify_chain_passes_over_survivors_after_purge(self, tmp_path: Path):
        """Whole-file purge never rewrites a surviving record's own bytes."""
        store = JSONLTraceStore(tmp_path)
        # Force several rotated days by writing files directly (store only
        # rotates in real time), then let today's store append normally.
        _write_file(tmp_path / "traces", _days_ago(10))
        _write_file(tmp_path / "traces", _days_ago(5))
        await store.append(TraceRecord(provider="anthropic", model="claude", trace_id="live-1"))

        deleted = await purge(tmp_path / "traces", max_age_days=7, max_bytes=None)

        assert len(deleted) == 1
        assert await store.verify_chain() is True


class TestPurgeConcurrentAppendRace:
    async def test_purge_never_touches_live_file_during_concurrent_append(self, tmp_path: Path):
        traces_dir = tmp_path / "traces"
        old_file = _write_file(traces_dir, _days_ago(10))
        store = JSONLTraceStore(tmp_path)

        async def _appender() -> None:
            for i in range(20):
                await store.append(
                    TraceRecord(provider="anthropic", model="claude", trace_id=f"live-{i}")
                )
                await asyncio.sleep(0)

        async def _purger() -> list[Path]:
            results: list[Path] = []
            for _ in range(20):
                results.extend(await purge(traces_dir, max_age_days=5, max_bytes=None))
                await asyncio.sleep(0)
            return results

        appender_task = asyncio.create_task(_appender())
        purger_task = asyncio.create_task(_purger())
        await asyncio.gather(appender_task, purger_task)

        live_file = traces_dir / f"traces-{_today()}.jsonl"
        assert live_file.exists()
        assert not old_file.exists()
        # The live file's line count reflects every concurrent append —
        # purge never stole or corrupted a line from it.
        lines = [ln for ln in live_file.read_text().strip().split("\n") if ln]
        assert len(lines) == 20
        assert await store.verify_chain() is True

    async def test_purge_bounds_batch_size(self, tmp_path: Path):
        traces_dir = tmp_path
        for i in range(5):
            _write_file(traces_dir, _days_ago(10 + i))

        deleted = await purge(traces_dir, max_age_days=5, max_bytes=None, max_files_per_run=2)

        assert len(deleted) == 2


class TestBuildCheckpoint:
    def test_checkpoint_reflects_records_present(self, tmp_path: Path):
        _write_file(tmp_path, _days_ago(3), n_records=2)
        _write_file(tmp_path, _days_ago(2), n_records=3)

        checkpoint = build_checkpoint(tmp_path)

        assert checkpoint["record_count"] == 5
        assert len(checkpoint["files"]) == 2
        assert checkpoint["head_hash"] != "0" * 64

    def test_checkpoint_has_timestamp(self, tmp_path: Path):
        _write_file(tmp_path, _days_ago(1), n_records=1)

        checkpoint = build_checkpoint(tmp_path)

        assert "timestamp" in checkpoint
        # Must parse as an ISO 8601 UTC timestamp.
        parsed = datetime.fromisoformat(checkpoint["timestamp"])
        assert parsed.tzinfo is not None

    def test_checkpoint_detects_purge_of_a_rotated_file(self, tmp_path: Path):
        """A shrinking file inventory reveals a purge that verify_chain() alone cannot see."""
        _write_file(tmp_path, _days_ago(10))
        _write_file(tmp_path, _days_ago(2))

        before = build_checkpoint(tmp_path)
        assert len(before["files"]) == 2

        asyncio.run(purge(tmp_path, max_age_days=5, max_bytes=None))

        after = build_checkpoint(tmp_path)
        assert len(after["files"]) == 1
        assert after["files"] != before["files"]

    def test_empty_directory_checkpoint(self, tmp_path: Path):
        checkpoint = build_checkpoint(tmp_path)
        assert checkpoint["head_hash"] == "0" * 64
        assert checkpoint["record_count"] == 0
        assert checkpoint["files"] == []

    def test_checkpoint_skips_blank_and_malformed_lines(self, tmp_path: Path):
        path = _write_file(tmp_path, _days_ago(1), n_records=1)
        with path.open("a") as f:
            f.write("\n")  # blank line
            f.write("{not valid json\n")  # malformed line

        checkpoint = build_checkpoint(tmp_path)
        assert checkpoint["record_count"] == 1


class TestVerifyAgainstAnchor:
    """The security-critical invariant: head-hash PRESENCE, not count/superset."""

    def test_true_when_anchored_head_is_present(self, tmp_path: Path):
        _write_file(tmp_path, _days_ago(3), n_records=2)
        _write_file(tmp_path, _days_ago(1), n_records=2)
        anchor = build_checkpoint(tmp_path)

        assert verify_against_anchor(tmp_path, anchor) is True

    def test_false_when_anchored_head_is_truncated_away(self, tmp_path: Path):
        """The whole point: removing the anchored head is detected."""
        _write_file(tmp_path, _days_ago(3), n_records=2)
        _write_file(tmp_path, _days_ago(1), n_records=2)
        anchor = build_checkpoint(tmp_path)

        # Simulate a malicious rollback: delete every file (removes the
        # anchored head entirely, even though a fresh chain would still
        # self-verify from its own new genesis).
        for f in tmp_path.glob("traces-*.jsonl"):
            f.unlink()

        assert verify_against_anchor(tmp_path, anchor) is False

    def test_true_after_legitimate_purge_of_older_files(self, tmp_path: Path):
        """Legitimate retention purge deletes only the OLDEST files — the
        recently anchored head survives it (tolerated, not flagged)."""
        _write_file(tmp_path, _days_ago(10), n_records=2)
        recent = _write_file(tmp_path, _days_ago(1), n_records=2)
        anchor = build_checkpoint(tmp_path)
        assert anchor["head_hash"] != "0" * 64

        deleted = asyncio.run(purge(tmp_path, max_age_days=5, max_bytes=None))

        assert deleted == [tmp_path / f"traces-{_days_ago(10)}.jsonl"]
        assert recent.exists()
        assert verify_against_anchor(tmp_path, anchor) is True

    def test_true_when_anchor_head_is_genesis(self, tmp_path: Path):
        """Nothing was anchored yet — vacuously nothing to attest."""
        anchor = {"head_hash": "0" * 64, "record_count": 0, "files": []}

        assert verify_against_anchor(tmp_path, anchor) is True

    def test_skips_blank_and_malformed_lines(self, tmp_path: Path):
        """A blank line and a malformed JSON line before the match are
        skipped, not fatal — the anchored head is still found."""
        path = _write_file(tmp_path, _days_ago(1), n_records=1)
        anchor = build_checkpoint(tmp_path)
        good_line = path.read_text().strip()
        # Prepend a malformed line, then a blank line, before the real
        # record — .strip() only trims the file's outer edges, so a blank
        # *middle* line survives to exercise the ``if not line`` skip.
        path.write_text(f"{{not valid json\n\n{good_line}\n")

        assert verify_against_anchor(tmp_path, anchor) is True

    def test_false_does_not_false_positive_on_record_count_or_files_shrinking(
        self, tmp_path: Path
    ):
        """Record-count/files shrinking from a legitimate purge must NOT
        by itself cause a false positive — only head-hash absence does."""
        _write_file(tmp_path, _days_ago(10), n_records=5)
        _write_file(tmp_path, _days_ago(1), n_records=1)
        anchor = build_checkpoint(tmp_path)

        asyncio.run(purge(tmp_path, max_age_days=5, max_bytes=None))
        after = build_checkpoint(tmp_path)

        # Record count and file inventory both legitimately shrank...
        assert after["record_count"] < anchor["record_count"]
        assert after["files"] != anchor["files"]
        # ...yet the anchored head (from the surviving recent file) is
        # still present, so verification still passes.
        assert verify_against_anchor(tmp_path, anchor) is True


class TestSafeDeleteFailure:
    async def test_purge_survives_a_failed_delete(self, tmp_path: Path, monkeypatch):
        """A single file that fails to delete is logged, not raised — purge continues."""
        from pathlib import Path as PathType

        stubborn = _write_file(tmp_path, _days_ago(10))
        other = _write_file(tmp_path, _days_ago(9))

        real_unlink = PathType.unlink

        def _flaky_unlink(self: Path, *args: object, **kwargs: object) -> None:
            if self == stubborn:
                raise OSError("permission denied")
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(PathType, "unlink", _flaky_unlink)

        deleted = await purge(tmp_path, max_age_days=5, max_bytes=None)

        assert stubborn.exists()
        assert other not in {stubborn}
        assert not other.exists()
        assert stubborn not in deleted
        assert other in deleted


class TestLoadTelemetryRetentionConfig:
    def test_default_is_unlimited(self):
        from arcllm.config import load_telemetry_retention_config

        cfg = load_telemetry_retention_config()
        assert cfg.max_age_days is None
        assert cfg.max_bytes is None

    def test_reads_configured_values(self, monkeypatch, tmp_path: Path):
        from arcllm import config as config_mod
        from arcllm.config import GlobalConfig, ModuleConfig, load_telemetry_retention_config

        mock_global = GlobalConfig(
            defaults={"provider": "anthropic", "temperature": 0.7, "max_tokens": 4096},
            modules={
                "telemetry": ModuleConfig(
                    enabled=True, retention={"max_age_days": 30, "max_bytes": 5000}
                )
            },
        )
        monkeypatch.setattr(config_mod, "load_global_config", lambda: mock_global)
        cfg = load_telemetry_retention_config()
        assert cfg.max_age_days == 30
        assert cfg.max_bytes == 5000

    def test_invalid_retention_config_raises(self, monkeypatch):
        from arcllm import config as config_mod
        from arcllm.config import GlobalConfig, ModuleConfig, load_telemetry_retention_config
        from arcllm.exceptions import ArcLLMConfigError

        mock_global = GlobalConfig(
            defaults={"provider": "anthropic", "temperature": 0.7, "max_tokens": 4096},
            modules={"telemetry": ModuleConfig(enabled=True, retention={"bogus_key": 1})},
        )
        monkeypatch.setattr(config_mod, "load_global_config", lambda: mock_global)
        with pytest.raises(ArcLLMConfigError, match="Invalid telemetry retention config"):
            load_telemetry_retention_config()

    def test_no_telemetry_module_configured_returns_unlimited(self, monkeypatch):
        from arcllm import config as config_mod
        from arcllm.config import GlobalConfig, load_telemetry_retention_config

        mock_global = GlobalConfig(
            defaults={"provider": "anthropic", "temperature": 0.7, "max_tokens": 4096},
            modules={},
        )
        monkeypatch.setattr(config_mod, "load_global_config", lambda: mock_global)
        cfg = load_telemetry_retention_config()
        assert cfg.max_age_days is None
        assert cfg.max_bytes is None


class TestJSONLTraceStoreRetentionWiring:
    async def test_store_accepts_retention_kwargs(self, tmp_path: Path):
        """Optional retention kwargs are accepted and stored (T16.7 wiring)."""
        store = JSONLTraceStore(tmp_path, retention_max_age_days=30, retention_max_bytes=1_000_000)
        assert store._retention_max_age_days == 30
        assert store._retention_max_bytes == 1_000_000

    async def test_store_without_retention_kwargs_never_purges(self, tmp_path: Path):
        """No retention configured (default) => rotation never invokes purge."""
        old_file = _write_file(tmp_path / "traces", _days_ago(1000))
        store = JSONLTraceStore(tmp_path)
        await store._maybe_purge()
        assert old_file.exists()

    async def test_maybe_purge_deletes_aged_files_when_configured(self, tmp_path: Path):
        """_maybe_purge actually invokes trace_retention.purge when configured."""
        old_file = _write_file(tmp_path / "traces", _days_ago(1000))
        store = JSONLTraceStore(tmp_path, retention_max_age_days=5)
        await store._maybe_purge()
        assert not old_file.exists()

    async def test_maybe_purge_logs_and_swallows_purge_failure(self, tmp_path: Path, monkeypatch):
        """A purge failure is logged, never raised — append() must never break."""
        import arcllm.trace_retention as retention_mod

        store = JSONLTraceStore(tmp_path, retention_max_age_days=5)

        async def _boom(*args: object, **kwargs: object) -> list[Path]:
            raise RuntimeError("disk unavailable")

        monkeypatch.setattr(retention_mod, "purge", _boom)
        await store._maybe_purge()  # must not raise
