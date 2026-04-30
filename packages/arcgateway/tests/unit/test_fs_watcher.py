"""Tests for arcgateway.fs_watcher — ref-counted watcher manager + mtime polling fallback."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from arcgateway.file_events import FileChangeEvent, FileEventBus
from arcgateway.fs_watcher import WatcherManager, match_event_type


@pytest.fixture
def agent_root(tmp_path: Path) -> Path:
    root = tmp_path / "team" / "alice_agent"
    (root / "workspace").mkdir(parents=True)
    (root / "workspace" / "policy.md").write_text(
        "- [P01] T {score:5, uses:0, reviewed:2026-04-01, created:2026-04-01, source:s}\n",
        encoding="utf-8",
    )
    (root / "arcagent.toml").write_text("[agent]\nname='alice'\n", encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# match_event_type — pure mapping
# ---------------------------------------------------------------------------


class TestMatchEventType:
    def test_arcagent_toml_maps_to_config_updated(self) -> None:
        assert match_event_type("arcagent.toml") == "config:updated"

    def test_policy_md_maps_to_policy_bullets_updated(self) -> None:
        assert match_event_type("workspace/policy.md") == "policy:bullets_updated"

    def test_identity_md_maps_to_memory_updated(self) -> None:
        assert match_event_type("workspace/identity.md") == "memory:updated"

    def test_subdir_path_matches_dir_prefix(self) -> None:
        assert match_event_type("workspace/memory/note.md") == "memory:updated"

    def test_session_jsonl_under_sessions_dir(self) -> None:
        assert match_event_type("workspace/sessions/s-2026-04-01.jsonl") == "session:changed"

    def test_unrelated_path_returns_none(self) -> None:
        assert match_event_type("workspace/unrelated_file.txt") is None
        assert match_event_type("random/path") is None

    def test_pulse_md(self) -> None:
        assert match_event_type("workspace/pulse.md") == "pulse:updated"

    def test_skills_dir(self) -> None:
        assert match_event_type("workspace/skills/my_skill.md") == "skills:updated"

    def test_tasks_json(self) -> None:
        assert match_event_type("workspace/tasks.json") == "tasks:updated"


# ---------------------------------------------------------------------------
# WatcherManager — ref-counted lifecycle
# ---------------------------------------------------------------------------


class TestRefCount:
    @pytest.mark.asyncio
    async def test_subscribe_starts_watcher(self, agent_root: Path) -> None:
        bus = FileEventBus()
        mgr = WatcherManager(bus=bus, force_polling=True, poll_interval=0.05)
        try:
            await mgr.subscribe("alice", agent_root)
            assert mgr.has_watcher("alice")
            assert mgr.refcount("alice") == 1
        finally:
            await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_double_subscribe_increments_refcount(self, agent_root: Path) -> None:
        bus = FileEventBus()
        mgr = WatcherManager(bus=bus, force_polling=True, poll_interval=0.05)
        try:
            await mgr.subscribe("alice", agent_root)
            await mgr.subscribe("alice", agent_root)
            assert mgr.refcount("alice") == 2
        finally:
            await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_unsubscribe_to_zero_stops_watcher(self, agent_root: Path) -> None:
        bus = FileEventBus()
        mgr = WatcherManager(bus=bus, force_polling=True, poll_interval=0.05)
        await mgr.subscribe("alice", agent_root)
        await mgr.subscribe("alice", agent_root)
        await mgr.unsubscribe("alice")
        assert mgr.has_watcher("alice") is True
        await mgr.unsubscribe("alice")
        # Allow task cancellation to propagate.
        await asyncio.sleep(0.05)
        assert mgr.has_watcher("alice") is False

    @pytest.mark.asyncio
    async def test_unsubscribe_unknown_is_noop(self) -> None:
        bus = FileEventBus()
        mgr = WatcherManager(bus=bus, force_polling=True)
        # Must not raise.
        await mgr.unsubscribe("nobody")

    @pytest.mark.asyncio
    async def test_max_watchers_cap(self, agent_root: Path) -> None:
        bus = FileEventBus()
        mgr = WatcherManager(bus=bus, force_polling=True, max_watchers=2, poll_interval=0.05)
        try:
            await mgr.subscribe("a", agent_root)
            await mgr.subscribe("b", agent_root)
            with pytest.raises(RuntimeError, match="max watchers"):
                await mgr.subscribe("c", agent_root)
        finally:
            await mgr.shutdown()


# ---------------------------------------------------------------------------
# WatcherManager — polling fallback emits events
# ---------------------------------------------------------------------------


class TestPollingFallback:
    @pytest.mark.asyncio
    async def test_modifying_policy_md_emits_event(self, agent_root: Path) -> None:
        bus = FileEventBus()
        received: list[FileChangeEvent] = []

        async def listener(evt: FileChangeEvent) -> None:
            received.append(evt)

        bus.subscribe(listener)
        mgr = WatcherManager(bus=bus, force_polling=True, poll_interval=0.05)
        try:
            await mgr.subscribe("alice", agent_root)
            # Give the watcher a tick to record its baseline.
            await asyncio.sleep(0.15)
            (agent_root / "workspace" / "policy.md").write_text(
                "- [P02] New {score:7, uses:1, reviewed:2026-04-29, created:2026-04-29, source:s}\n",
                encoding="utf-8",
            )
            # Wait for the next poll round.
            for _ in range(40):
                if any(e.event_type == "policy:bullets_updated" for e in received):
                    break
                await asyncio.sleep(0.05)
            assert any(e.event_type == "policy:bullets_updated" for e in received)
        finally:
            await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_creating_new_session_jsonl_emits(self, agent_root: Path) -> None:
        bus = FileEventBus()
        received: list[FileChangeEvent] = []

        async def listener(evt: FileChangeEvent) -> None:
            received.append(evt)

        bus.subscribe(listener)
        sess_dir = agent_root / "workspace" / "sessions"
        sess_dir.mkdir()
        mgr = WatcherManager(bus=bus, force_polling=True, poll_interval=0.05)
        try:
            await mgr.subscribe("alice", agent_root)
            await asyncio.sleep(0.15)
            (sess_dir / "s-new.jsonl").write_text("{}\n", encoding="utf-8")
            for _ in range(40):
                if any(e.event_type == "session:changed" for e in received):
                    break
                await asyncio.sleep(0.05)
            assert any(e.event_type == "session:changed" for e in received)
        finally:
            await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_policy_payload_empty_when_policy_md_missing(
        self, agent_root: Path
    ) -> None:
        # Remove policy.md so the bullets payload path takes the fallback.
        (agent_root / "workspace" / "policy.md").unlink()
        # Recreate it so the watcher has something to detect a change.
        (agent_root / "workspace" / "policy.md").write_text("# blank\n", encoding="utf-8")

        bus = FileEventBus()
        received: list[FileChangeEvent] = []

        async def listener(evt: FileChangeEvent) -> None:
            received.append(evt)

        bus.subscribe(listener)
        mgr = WatcherManager(bus=bus, force_polling=True, poll_interval=0.05)
        try:
            await mgr.subscribe("alice", agent_root)
            await asyncio.sleep(0.15)
            # Re-write to a non-bullet payload to trigger an event.
            (agent_root / "workspace" / "policy.md").write_text("# updated\n", encoding="utf-8")
            for _ in range(40):
                if any(e.event_type == "policy:bullets_updated" for e in received):
                    break
                await asyncio.sleep(0.05)
            policy_evts = [e for e in received if e.event_type == "policy:bullets_updated"]
            assert policy_evts
            assert policy_evts[-1].payload.get("bullets") == []
        finally:
            await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_dispatch_ignores_unrelated_paths(self, agent_root: Path) -> None:
        bus = FileEventBus()
        received: list[FileChangeEvent] = []

        async def listener(evt: FileChangeEvent) -> None:
            received.append(evt)

        bus.subscribe(listener)
        # Add a file the watcher doesn't track so iter_watched_files won't include it.
        # We dispatch directly to exercise the unmatched-path branch.
        mgr = WatcherManager(bus=bus, force_polling=True, poll_interval=0.05)
        await mgr.subscribe("alice", agent_root)
        try:
            from arcgateway.fs_watcher import _WatcherEntry

            entry = _WatcherEntry(agent_id="alice", agent_root=agent_root.resolve())
            unrelated = agent_root / "random.txt"
            unrelated.write_text("hi", encoding="utf-8")
            outside = agent_root.parent / "elsewhere.txt"
            outside.write_text("hi", encoding="utf-8")

            await mgr._dispatch(entry, unrelated)  # path inside root but unmapped
            await mgr._dispatch(entry, outside)  # path outside root → ValueError branch
            # No events should have been emitted from these dispatch calls.
            assert all(e.event_type != "config:updated" for e in received)
        finally:
            await mgr.shutdown()


class TestRenderPolicyPayload:
    @pytest.mark.asyncio
    async def test_returns_empty_list_when_policy_md_missing(self, agent_root: Path) -> None:
        # Delete policy.md entirely so read_file raises FileNotFoundError.
        (agent_root / "workspace" / "policy.md").unlink()
        bus = FileEventBus()
        mgr = WatcherManager(bus=bus, force_polling=True, poll_interval=0.05)

        from arcgateway.fs_watcher import _WatcherEntry

        entry = _WatcherEntry(agent_id="alice", agent_root=agent_root.resolve())
        result = mgr._render_policy_payload(entry)
        assert result == []


class TestShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_cancels_all(self, agent_root: Path) -> None:
        bus = FileEventBus()
        mgr = WatcherManager(bus=bus, force_polling=True, poll_interval=0.05)
        await mgr.subscribe("a", agent_root)
        await mgr.subscribe("b", agent_root)
        await mgr.shutdown()
        assert not mgr.has_watcher("a")
        assert not mgr.has_watcher("b")

    @pytest.mark.asyncio
    async def test_shutdown_idempotent(self, agent_root: Path) -> None:
        bus = FileEventBus()
        mgr = WatcherManager(bus=bus, force_polling=True, poll_interval=0.05)
        await mgr.subscribe("a", agent_root)
        await mgr.shutdown()
        # Should not raise.
        await mgr.shutdown()


class TestPolicyPayloadRendering:
    @pytest.mark.asyncio
    async def test_policy_event_includes_parsed_bullets_payload(
        self, agent_root: Path
    ) -> None:
        bus = FileEventBus()
        received: list[FileChangeEvent] = []

        async def listener(evt: FileChangeEvent) -> None:
            received.append(evt)

        bus.subscribe(listener)
        mgr = WatcherManager(bus=bus, force_polling=True, poll_interval=0.05)
        try:
            await mgr.subscribe("alice", agent_root)
            await asyncio.sleep(0.15)
            (agent_root / "workspace" / "policy.md").write_text(
                "- [P99] FRESH {score:9, uses:5, reviewed:2026-04-29, created:2026-01-01, source:s-z}\n",
                encoding="utf-8",
            )
            for _ in range(40):
                policy_evts = [e for e in received if e.event_type == "policy:bullets_updated"]
                if policy_evts and policy_evts[-1].payload.get("bullets"):
                    break
                await asyncio.sleep(0.05)
            policy_evts = [e for e in received if e.event_type == "policy:bullets_updated"]
            assert policy_evts, "expected at least one policy:bullets_updated event"
            bullets = policy_evts[-1].payload.get("bullets")
            assert bullets and any(b["id"] == "P99" for b in bullets)
        finally:
            await mgr.shutdown()
