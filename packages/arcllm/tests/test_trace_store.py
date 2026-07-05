"""Tests for TraceStore — TraceRecord, JSONLTraceStore, hash chain verification."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from arcllm.trace_store import JSONLTraceStore, TraceRecord

# ---------------------------------------------------------------------------
# TraceRecord tests (Task 1.1)
# ---------------------------------------------------------------------------


class TestTraceRecord:
    def test_default_fields(self):
        rec = TraceRecord(provider="anthropic", model="claude-sonnet-4")
        assert rec.provider == "anthropic"
        assert rec.model == "claude-sonnet-4"
        assert rec.event_type == "llm_call"
        assert rec.status == "success"
        assert rec.prev_hash == "0" * 64
        assert rec.record_hash == ""
        assert len(rec.trace_id) == 32  # UUID4 hex

    def test_frozen(self):
        rec = TraceRecord(provider="anthropic", model="claude-sonnet-4")
        with pytest.raises(ValidationError):
            rec.provider = "openai"  # type: ignore[misc]

    def test_compute_hash_deterministic(self):
        rec = TraceRecord(
            trace_id="abc123",
            timestamp="2026-03-01T00:00:00+00:00",
            provider="anthropic",
            model="claude-sonnet-4",
            duration_ms=100.0,
            cost_usd=0.001,
            input_tokens=50,
            output_tokens=25,
            total_tokens=75,
        )
        h1 = rec.compute_hash()
        h2 = rec.compute_hash()
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex

    def test_compute_hash_changes_with_data(self):
        base = TraceRecord(
            trace_id="abc123",
            timestamp="2026-03-01T00:00:00+00:00",
            provider="anthropic",
            model="claude-sonnet-4",
        )
        modified = base.model_copy(update={"cost_usd": 1.0})
        assert base.compute_hash() != modified.compute_hash()

    def test_with_hash(self):
        rec = TraceRecord(provider="anthropic", model="claude-sonnet-4")
        hashed = rec.with_hash("a" * 64)
        assert hashed.prev_hash == "a" * 64
        assert hashed.record_hash != ""
        assert len(hashed.record_hash) == 64

    def test_with_hash_chain(self):
        r1 = TraceRecord(
            trace_id="001",
            timestamp="2026-03-01T00:00:00+00:00",
            provider="anthropic",
            model="claude-sonnet-4",
        ).with_hash("0" * 64)

        r2 = TraceRecord(
            trace_id="002",
            timestamp="2026-03-01T00:00:01+00:00",
            provider="anthropic",
            model="claude-sonnet-4",
        ).with_hash(r1.record_hash)

        assert r2.prev_hash == r1.record_hash
        assert r2.record_hash != r1.record_hash

    def test_serialization_roundtrip(self):
        rec = TraceRecord(
            provider="anthropic",
            model="claude-sonnet-4",
            request_body={"messages": [{"role": "user", "content": "hi"}]},
            response_body={"content": "hello"},
            phase_timings={"llm_call_ms": 150.5},
        ).with_hash("0" * 64)

        data = rec.model_dump()
        restored = TraceRecord(**data)
        assert restored.provider == rec.provider
        assert restored.record_hash == rec.record_hash
        assert restored.request_body == rec.request_body
        assert restored.phase_timings == rec.phase_timings

    def test_json_roundtrip(self):
        rec = TraceRecord(
            provider="openai",
            model="gpt-4o",
            cost_usd=0.015,
        ).with_hash("0" * 64)

        json_str = json.dumps(rec.model_dump())
        data = json.loads(json_str)
        restored = TraceRecord(**data)
        assert restored.record_hash == rec.record_hash

    def test_config_change_event_type(self):
        rec = TraceRecord(
            provider="system",
            model="system",
            event_type="config_change",
            event_data={"actor": "operator", "changes": {"temperature": {"old": 0.7, "new": 0.5}}},
        )
        assert rec.event_type == "config_change"
        assert rec.event_data is not None
        assert rec.event_data["actor"] == "operator"

    def test_circuit_change_event_type(self):
        rec = TraceRecord(
            provider="anthropic",
            model="claude-sonnet-4",
            event_type="circuit_change",
            event_data={"old_state": "closed", "new_state": "open"},
        )
        assert rec.event_type == "circuit_change"


# ---------------------------------------------------------------------------
# JSONLTraceStore tests (Task 1.2, 1.3)
# ---------------------------------------------------------------------------


class TestJSONLTraceStore:
    @pytest.fixture
    def agent_root(self, tmp_path: Path) -> Path:
        return tmp_path / "agent_a"

    @pytest.fixture
    def store(self, agent_root: Path) -> JSONLTraceStore:
        return JSONLTraceStore(agent_root)

    def _make_record(self, **kwargs: object) -> TraceRecord:
        defaults: dict[str, object] = {
            "provider": "anthropic",
            "model": "claude-sonnet-4",
            "duration_ms": 100.0,
            "cost_usd": 0.001,
            "input_tokens": 50,
            "output_tokens": 25,
            "total_tokens": 75,
        }
        defaults.update(kwargs)
        return TraceRecord(**defaults)  # type: ignore[arg-type]

    async def test_append_creates_file(self, store: JSONLTraceStore, agent_root: Path):
        rec = self._make_record()
        await store.append(rec)

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        file_path = agent_root / "traces" / f"traces-{today}.jsonl"
        assert file_path.exists()

        lines = file_path.read_text().strip().split("\n")
        assert len(lines) == 1

        data = json.loads(lines[0])
        assert data["provider"] == "anthropic"
        assert data["record_hash"] != ""
        assert data["prev_hash"] == "0" * 64

    async def test_append_chains_hashes(self, store: JSONLTraceStore, agent_root: Path):
        r1 = self._make_record(trace_id="001")
        r2 = self._make_record(trace_id="002")
        r3 = self._make_record(trace_id="003")

        await store.append(r1)
        await store.append(r2)
        await store.append(r3)

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        file_path = agent_root / "traces" / f"traces-{today}.jsonl"
        lines = file_path.read_text().strip().split("\n")
        assert len(lines) == 3

        d1 = json.loads(lines[0])
        d2 = json.loads(lines[1])
        d3 = json.loads(lines[2])

        assert d1["prev_hash"] == "0" * 64
        assert d2["prev_hash"] == d1["record_hash"]
        assert d3["prev_hash"] == d2["record_hash"]

    async def test_verify_chain_valid(self, store: JSONLTraceStore):
        for i in range(5):
            await store.append(self._make_record(trace_id=f"rec-{i}"))

        assert await store.verify_chain() is True

    async def test_verify_chain_detects_tampering(self, store: JSONLTraceStore, agent_root: Path):
        for i in range(3):
            await store.append(self._make_record(trace_id=f"rec-{i}"))

        # Tamper with second record
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        file_path = agent_root / "traces" / f"traces-{today}.jsonl"
        lines = file_path.read_text().strip().split("\n")
        data = json.loads(lines[1])
        data["cost_usd"] = 999.99  # Tamper!
        lines[1] = json.dumps(data)
        file_path.write_text("\n".join(lines) + "\n")

        assert await store.verify_chain() is False

    async def test_query_returns_newest_first(self, store: JSONLTraceStore):
        for i in range(5):
            await store.append(
                self._make_record(
                    trace_id=f"rec-{i:03d}",
                    timestamp=f"2026-03-01T00:00:{i:02d}+00:00",
                )
            )

        results, cursor = await store.query(limit=3)
        assert len(results) == 3
        assert results[0].trace_id == "rec-004"
        assert results[1].trace_id == "rec-003"
        assert results[2].trace_id == "rec-002"
        assert cursor is not None

    async def test_query_with_cursor_pagination(self, store: JSONLTraceStore):
        for i in range(5):
            await store.append(
                self._make_record(
                    trace_id=f"rec-{i:03d}",
                    timestamp=f"2026-03-01T00:00:{i:02d}+00:00",
                )
            )

        page1, cursor1 = await store.query(limit=2)
        assert len(page1) == 2
        assert cursor1 is not None

        page2, _cursor2 = await store.query(limit=2, cursor=cursor1)
        assert len(page2) == 2

        # All pages should have distinct trace_ids
        all_ids = [r.trace_id for r in page1 + page2]
        assert len(set(all_ids)) == 4

    async def test_query_filter_provider(self, store: JSONLTraceStore):
        await store.append(self._make_record(provider="anthropic"))
        await store.append(self._make_record(provider="openai"))
        await store.append(self._make_record(provider="anthropic"))

        results, _ = await store.query(provider="openai")
        assert len(results) == 1
        assert results[0].provider == "openai"

    async def test_query_filter_agent(self, store: JSONLTraceStore):
        await store.append(self._make_record(agent_label="agent-1"))
        await store.append(self._make_record(agent_label="agent-2"))
        await store.append(self._make_record(agent_label="agent-1"))

        results, _ = await store.query(agent="agent-1")
        assert len(results) == 2

    async def test_query_filter_status(self, store: JSONLTraceStore):
        await store.append(self._make_record(status="success"))
        await store.append(self._make_record(status="error", error="timeout"))
        await store.append(self._make_record(status="success"))

        results, _ = await store.query(status="error")
        assert len(results) == 1
        assert results[0].status == "error"

    async def test_get_by_trace_id(self, store: JSONLTraceStore):
        await store.append(self._make_record(trace_id="target-id"))
        await store.append(self._make_record(trace_id="other-id"))

        result = await store.get("target-id")
        assert result is not None
        assert result.trace_id == "target-id"

    async def test_get_not_found(self, store: JSONLTraceStore):
        await store.append(self._make_record(trace_id="exists"))
        result = await store.get("does-not-exist")
        assert result is None

    async def test_warm_start_from_existing_file(self, agent_root: Path):
        # Write some records manually
        store1 = JSONLTraceStore(agent_root)
        for i in range(3):
            await store1.append(self._make_record(trace_id=f"old-{i}"))

        # Create a new store instance (simulates restart)
        store2 = JSONLTraceStore(agent_root)
        await store2.append(self._make_record(trace_id="new-0"))

        # Chain should still be valid across both stores
        assert await store2.verify_chain() is True

    async def test_close_is_noop(self, store: JSONLTraceStore):
        await store.close()  # Should not raise

    async def test_query_skips_rotation_tombstones(self, store: JSONLTraceStore):
        await store.append(self._make_record(trace_id="real-record"))

        # Manually append a rotation tombstone
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        file_path = store._traces_dir / f"traces-{today}.jsonl"
        tombstone = TraceRecord(
            trace_id="tombstone",
            provider="system",
            model="system",
            event_type="rotation",
            event_data={"next_file": "traces-tomorrow.jsonl"},
        ).with_hash(store._last_hash)
        with file_path.open("a") as f:
            f.write(json.dumps(tombstone.model_dump()) + "\n")

        results, _ = await store.query()
        assert len(results) == 1
        assert results[0].trace_id == "real-record"


# ---------------------------------------------------------------------------
# Trace location (NIST AU-9 — sibling to workspace, not inside it)
# ---------------------------------------------------------------------------


class TestTraceLocation:
    def test_traces_at_agent_root(self, tmp_path: Path) -> None:
        agent = tmp_path / "agent_a"
        store = JSONLTraceStore(agent)
        assert store._traces_dir == agent / "traces"


# ---------------------------------------------------------------------------
# M3 — blocking hash/serialize/write work must run off the event loop
# ---------------------------------------------------------------------------


class TestAppendOffloadsBlockingWork:
    async def test_append_frees_event_loop_during_write(self, tmp_path: Path) -> None:
        """The hash+serialize+write happens off the event loop, inside the
        chain-ordering lock. A concurrent coroutine must be able to make
        progress while the write is in flight — forced interleaving via a
        real threading.Event, not an instant mock (a synchronous write
        would deadlock this test rather than merely run slower)."""
        import asyncio
        import threading
        from unittest.mock import patch

        store = JSONLTraceStore(tmp_path)
        write_entered = threading.Event()
        release_write = threading.Event()
        real_write = JSONLTraceStore._write_record_sync

        def _blocking_write(record: TraceRecord, prev_hash: str, file_path: Path) -> TraceRecord:
            write_entered.set()
            assert release_write.wait(timeout=5), "writer never released — event loop blocked"
            return real_write(record, prev_hash, file_path)

        marker = {"ran": False}

        async def _concurrent_marker() -> None:
            loop = asyncio.get_event_loop()
            started = await loop.run_in_executor(None, write_entered.wait, 5)
            assert started, "write never started"
            marker["ran"] = True
            release_write.set()

        with patch.object(JSONLTraceStore, "_write_record_sync", staticmethod(_blocking_write)):
            await asyncio.wait_for(
                asyncio.gather(
                    store.append(TraceRecord(provider="anthropic", model="claude")),
                    _concurrent_marker(),
                ),
                timeout=5,
            )

        assert marker["ran"] is True

    async def test_warm_start_reads_file_via_thread(self, tmp_path: Path) -> None:
        """_warm_start's file read is dispatched to a worker thread."""
        import asyncio
        from unittest.mock import patch

        store = JSONLTraceStore(tmp_path)
        await store.append(TraceRecord(provider="anthropic", model="claude"))
        store._warm_started = False  # force _warm_start to run again

        calls: list[object] = []
        orig_to_thread = asyncio.to_thread

        async def _spy(func, *args, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(func)
            return await orig_to_thread(func, *args, **kwargs)

        with patch("arcllm.trace_store.asyncio.to_thread", _spy):
            await store._warm_start()

        assert calls, "expected _warm_start to dispatch its file read via asyncio.to_thread"


# ---------------------------------------------------------------------------
# checkpoint_sink — trace-checkpoint signed-anchor wiring
# ---------------------------------------------------------------------------


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


class TestCheckpointSinkAnchor:
    async def test_checkpoint_sink_invoked_with_prepurge_checkpoint(self, tmp_path: Path) -> None:
        """The anchor captures the checkpoint BEFORE purge deletes anything."""
        traces_dir = tmp_path / "traces"
        old_date = (datetime.now(UTC) - timedelta(days=10)).strftime("%Y-%m-%d")
        old_file = _write_rotated_file(traces_dir, old_date)

        received: list[dict[str, object]] = []
        store = JSONLTraceStore(
            tmp_path, retention_max_age_days=5, checkpoint_sink=received.append
        )

        await store._maybe_purge()

        assert len(received) == 1
        checkpoint = received[0]
        files = checkpoint["files"]
        assert isinstance(files, list)
        assert old_file.name in files  # present in the checkpoint...
        assert not old_file.exists()  # ...even though purge then deleted it

    async def test_checkpoint_sink_fires_every_rotation_without_retention(
        self, tmp_path: Path
    ) -> None:
        """Anchoring is independent of whether retention purge is configured."""
        received: list[dict[str, object]] = []
        store = JSONLTraceStore(tmp_path, checkpoint_sink=received.append)

        await store._maybe_purge()

        assert len(received) == 1

    async def test_no_checkpoint_sink_is_a_noop(self, tmp_path: Path) -> None:
        store = JSONLTraceStore(tmp_path)
        await store._maybe_purge()  # must not raise

    async def test_checkpoint_sink_failure_is_swallowed(self, tmp_path: Path) -> None:
        """A signer/sink failure must never break capture (NIST AU-5)."""

        def _boom(_checkpoint: dict[str, object]) -> None:
            raise RuntimeError("signer unavailable")

        store = JSONLTraceStore(tmp_path, checkpoint_sink=_boom)

        await store._maybe_purge()  # must not raise

        # Capture still succeeds after an anchor failure.
        await store.append(
            TraceRecord(provider="anthropic", model="claude", trace_id="after-failure")
        )
        assert await store.verify_chain() is True
