"""Shared queue ownership and durable state regressions."""

import asyncio
import os
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from arctrust import AnchorHead, AnchorUnavailableError, RecordCipher

from arcllm.exceptions import ArcLLMConfigError, QueueFullError, QueueStateUnavailableError
from arcllm.modules.queue import QueueModule
from arcllm.queue_control import (
    CallJob,
    CallQueueContext,
    CallQueueCoordinator,
    MemoryQueueStore,
    QueueLimits,
    QueueState,
    _ProviderPool,
    _Waiter,
)
from arcllm.queue_journal import QueueJournal
from arcllm.registry import load_model
from arcllm.types import Delta, LLMProvider, LLMResponse, Message, Usage


class FakeAnchor:
    """Separate monotonic authority for journal contract tests."""

    def __init__(self) -> None:
        self.head: AnchorHead | None = None
        self.unavailable = False

    @property
    def scope(self) -> str:
        return "queue/test"

    def latest(self) -> AnchorHead | None:
        if self.unavailable:
            raise AnchorUnavailableError("remote authority unavailable")
        return self.head

    def compare_and_advance(
        self, expected: AnchorHead | None, digest: str, intent: str
    ) -> AnchorHead:
        if self.unavailable:
            raise AnchorUnavailableError("remote authority unavailable")
        if self.head != expected:
            raise AnchorUnavailableError("stale anchor version")
        self.head = AnchorHead(
            scope=self.scope,
            version=1 if expected is None else expected.version + 1,
            digest=digest,
            previous_digest=expected.digest if expected else None,
            intent=intent,
        )
        return self.head


@pytest.mark.asyncio
async def test_provider_capacity_is_shared_and_tenant_fair() -> None:
    coordinator = CallQueueCoordinator(limits=QueueLimits(max_concurrent=1, max_queued=4))
    entered: list[str] = []
    release = asyncio.Event()

    async def call(tenant: str) -> None:
        context = CallQueueContext(tenant_id=tenant, owner_id=tenant)
        async with coordinator.call(context) as job:
            async with coordinator.attempt("provider:primary", job):
                entered.append(tenant)
                if len(entered) == 1:
                    await release.wait()

    first = asyncio.create_task(call("a"))
    await asyncio.wait_for(_entered(entered), 1)
    other_a = asyncio.create_task(call("a"))
    other_b = asyncio.create_task(call("b"))
    await asyncio.sleep(0.01)
    assert entered == ["a"]
    release.set()
    await asyncio.gather(first, other_a, other_b)
    assert entered == ["a", "b", "a"]


async def _entered(entered: list[str]) -> None:
    while not entered:
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_journal_is_encrypted_and_recovery_marks_uncertain(tmp_path: Path) -> None:
    journal = QueueJournal(tmp_path / "calls.sqlite", RecordCipher(b"k" * 32), FakeAnchor())
    coordinator = CallQueueCoordinator(store=journal)
    await coordinator.initialize()
    context = CallQueueContext(tenant_id="tenant", owner_id="run", call_id="call-1")
    async with coordinator.call(context) as job:
        async with coordinator.attempt("provider:primary", job):
            row = await journal.get("call-1")
            assert row is not None and row.state == "running"
            raw = (tmp_path / "calls.sqlite").read_bytes()
            assert b"tenant" not in raw and b"provider:primary" not in raw
    row = await journal.get("call-1")
    assert row is not None and row.state == "completed"


@pytest.mark.asyncio
async def test_recovery_never_replays_or_accepts_stale_owner(tmp_path: Path) -> None:
    journal = QueueJournal(tmp_path / "calls.sqlite", RecordCipher(b"k" * 32), FakeAnchor())
    coordinator = CallQueueCoordinator(store=journal)
    await coordinator.initialize()
    context = CallQueueContext(tenant_id="tenant", owner_id="run", call_id="call-2")
    job = await coordinator.register(context)
    started = await journal.compare_and_set(job.call_id, job.version, "running", "run")
    assert started is not None
    recovered = await CallQueueCoordinator(store=journal).recover()
    assert recovered == 1
    row = await journal.get(job.call_id)
    assert row is not None and row.state == "outcome_unknown"
    assert await journal.compare_and_set(job.call_id, started.version, "completed", "run") is None


@pytest.mark.asyncio
async def test_configured_models_share_wire_capacity_and_keep_call_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arcllm.adapters.anthropic import AnthropicAdapter

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    coordinator = CallQueueCoordinator(limits=QueueLimits(max_concurrent=1, max_queued=1))
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def wire(*_args: object, **_kwargs: object) -> LLMResponse:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return LLMResponse(
            content="ok",
            model="test",
            stop_reason="end_turn",
            usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
        )

    with patch.object(AnthropicAdapter, "invoke", wire):
        first = load_model(
            "anthropic",
            queue_coordinator=coordinator,
            queue_context=CallQueueContext("tenant-a", "run-a", "call-a"),
            telemetry=True,
            security=False,
            retry=False,
        )
        second = load_model(
            "anthropic",
            queue_coordinator=coordinator,
            queue_context=CallQueueContext("tenant-b", "run-b", "call-b"),
            telemetry=False,
            security=False,
            retry=False,
        )
        try:
            one = asyncio.create_task(first.invoke([Message(role="user", content="hi")]))
            await entered.wait()
            two = asyncio.create_task(second.invoke([Message(role="user", content="hi")]))
            await asyncio.sleep(0.01)
            assert calls == 1
            jobs = await coordinator.jobs()
            assert {(job.call_id, job.state) for job in jobs} == {
                ("call-a", "running"),
                ("call-b", "queued"),
            }
            release.set()
            await asyncio.gather(one, two)
            assert calls == 2
            assert {job.state for job in await coordinator.jobs()} == {"completed"}
        finally:
            await first.close()
            await second.close()


@pytest.mark.asyncio
async def test_cached_model_uses_each_tasks_run_context_without_cross_talk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arcllm.adapters.anthropic import AnthropicAdapter

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    coordinator = CallQueueCoordinator(limits=QueueLimits(max_concurrent=2))
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def wire(*_args: object, **_kwargs: object) -> LLMResponse:
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
        await release.wait()
        return LLMResponse(
            content="ok",
            model="test",
            stop_reason="end_turn",
            usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
        )

    with patch.object(AnthropicAdapter, "invoke", wire):
        model = load_model(
            "anthropic",
            queue_coordinator=coordinator,
            telemetry=False,
            security=False,
            retry=False,
        )

        async def turn(tenant: str, run: str) -> None:
            context = CallQueueContext(tenant, f"owner-{run}", run_id=run, session_id=run)
            with coordinator.bind_context(context):
                await model.invoke([Message(role="user", content=run)])

        try:
            first = asyncio.create_task(turn("a", "run-a"))
            second = asyncio.create_task(turn("b", "run-b"))
            await asyncio.wait_for(entered.wait(), 1)
            release.set()
            await asyncio.gather(first, second)
            jobs = await coordinator.jobs()
            assert {(job.tenant_id, job.run_id, job.session_id) for job in jobs} == {
                ("a", "run-a", "run-a"),
                ("b", "run-b", "run-b"),
            }
            assert len({job.call_id for job in jobs}) == 2
        finally:
            release.set()
            await model.close()


def test_injected_shared_owner_cannot_be_disabled_by_module_config() -> None:
    from arcllm.exceptions import ArcLLMConfigError

    with pytest.raises(ArcLLMConfigError, match="requires the queue module"):
        load_model("anthropic", queue=False, queue_coordinator=CallQueueCoordinator())


def test_bound_run_context_rejects_call_site_identity_override() -> None:
    coordinator = CallQueueCoordinator()
    module = QueueModule({}, cast(LLMProvider, object()), coordinator=coordinator)
    bound = CallQueueContext("tenant", "owner", run_id="real")
    spoofed = CallQueueContext("tenant", "owner", run_id="spoofed")
    with coordinator.bind_context(bound):
        with pytest.raises(ValueError, match="conflicts with the bound run"):
            module._resolve_context(spoofed)
        assert module._resolve_context(None) == bound


def test_module_capacity_conflict_with_shared_owner_is_rejected() -> None:
    from arcllm.exceptions import ArcLLMConfigError

    with pytest.raises(ArcLLMConfigError, match="controlled by its coordinator"):
        load_model(
            "anthropic",
            queue={"max_concurrent": 3},
            queue_coordinator=CallQueueCoordinator(limits=QueueLimits(max_concurrent=1)),
        )


def test_shared_owner_limits_override_module_defaults() -> None:
    coordinator = CallQueueCoordinator(limits=QueueLimits(max_concurrent=3, max_queued=4))
    model = load_model("anthropic", queue_coordinator=coordinator)
    assert isinstance(model, QueueModule)
    assert model._max_concurrent == 3
    assert model._max_queued == 4


@pytest.mark.asyncio
async def test_fallback_uses_one_call_and_distinct_wire_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arcllm.adapters.anthropic import AnthropicAdapter
    from arcllm.adapters.openai import OpenaiAdapter

    class RecordingStore(MemoryQueueStore):
        def __init__(self) -> None:
            super().__init__()
            self.attempts: list[str] = []

        async def compare_and_set(
            self,
            call_id: str,
            version: int,
            state: QueueState,
            owner_id: str,
            *,
            provider_scope: str | None = None,
            attempt_id: str | None = None,
        ) -> CallJob | None:
            if attempt_id is not None:
                self.attempts.append(attempt_id)
            return await super().compare_and_set(
                call_id,
                version,
                state,
                owner_id,
                provider_scope=provider_scope,
                attempt_id=attempt_id,
            )

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    store = RecordingStore()
    coordinator = CallQueueCoordinator(store=store)

    async def primary(*_args: object, **_kwargs: object) -> LLMResponse:
        raise RuntimeError("provider unavailable")

    async def secondary(*_args: object, **_kwargs: object) -> LLMResponse:
        return LLMResponse(
            content="fallback",
            model="test",
            stop_reason="end_turn",
            usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
        )

    with (
        patch.object(AnthropicAdapter, "invoke", primary),
        patch.object(OpenaiAdapter, "invoke", secondary),
    ):
        model = load_model(
            "anthropic",
            queue_coordinator=coordinator,
            queue_context=CallQueueContext("tenant", "run", "call"),
            fallback={"chain": ["openai"]},
            telemetry=False,
            security=False,
            retry=False,
        )
        try:
            result = await model.invoke([Message(role="user", content="hi")])
            assert result.content == "fallback"
            assert len(store.attempts) == 2
            assert store.attempts[0] != store.attempts[1]
            jobs = await coordinator.jobs()
            assert len(jobs) == 1
            assert jobs[0].state == "completed"
            assert jobs[0].provider_scope == "openai"
        finally:
            await model.close()


async def test_cancel_yielded_stream_closes_provider_and_releases_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arcllm.adapters.anthropic import AnthropicAdapter

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    coordinator = CallQueueCoordinator(limits=QueueLimits(max_concurrent=1, max_queued=0))
    closed = asyncio.Event()

    async def wire(*_args: object, **_kwargs: object) -> AsyncIterator[Delta]:
        try:
            yield Delta(text="first")
            yield Delta(text="second")
        finally:
            closed.set()

    with patch.object(AnthropicAdapter, "invoke_stream", wire):
        model = load_model(
            "anthropic",
            queue_coordinator=coordinator,
            queue_context=CallQueueContext("tenant", "owner", "stream-call"),
            telemetry=False,
            security=False,
            retry=False,
        )
        try:
            stream = model.invoke_stream([Message(role="user", content="hi")])
            assert (await asyncio.create_task(anext(stream))).text == "first"
            assert await coordinator.cancel("stream-call", owner_id="owner")
            assert closed.is_set()
            assert (await coordinator.jobs())[0].state == "cancelled"
            await asyncio.create_task(stream.aclose())
            assert coordinator._pools["anthropic"].active == 0
        finally:
            await model.close()


@pytest.mark.asyncio
async def test_cancel_idle_stream_does_not_cancel_consumer_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arcllm.adapters.anthropic import AnthropicAdapter

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    coordinator = CallQueueCoordinator()
    yielded = asyncio.Event()
    continue_work = asyncio.Event()
    closed = asyncio.Event()

    async def wire(*_args: object, **_kwargs: object) -> AsyncIterator[Delta]:
        try:
            yield Delta(text="first")
            yield Delta(text="second")
        finally:
            closed.set()

    with patch.object(AnthropicAdapter, "invoke_stream", wire):
        model = load_model(
            "anthropic",
            queue_coordinator=coordinator,
            queue_context=CallQueueContext("tenant", "owner", "idle-stream"),
            telemetry=False,
            security=False,
            retry=False,
        )

        async def consumer() -> str:
            stream = model.invoke_stream([Message(role="user", content="hi")])
            try:
                first = await anext(stream)
                yielded.set()
                await continue_work.wait()
                return first.text
            finally:
                await stream.aclose()

        task = asyncio.create_task(consumer())
        try:
            await yielded.wait()
            assert await coordinator.cancel("idle-stream", owner_id="owner")
            assert closed.is_set()
            assert not task.cancelled() and not task.done()
            continue_work.set()
            assert await task == "first"
            assert (await coordinator.jobs())[0].state == "cancelled"
        finally:
            continue_work.set()
            await model.close()


@pytest.mark.asyncio
async def test_cancel_cross_task_stream_targets_current_provider_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arcllm.adapters.anthropic import AnthropicAdapter

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    coordinator = CallQueueCoordinator()
    waiting = asyncio.Event()
    closed = asyncio.Event()

    async def wire(*_args: object, **_kwargs: object) -> AsyncIterator[Delta]:
        try:
            yield Delta(text="first")
            waiting.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    with patch.object(AnthropicAdapter, "invoke_stream", wire):
        model = load_model(
            "anthropic",
            queue_coordinator=coordinator,
            queue_context=CallQueueContext("tenant", "owner", "cross-task"),
            telemetry=False,
            security=False,
            retry=False,
        )
        stream = model.invoke_stream([Message(role="user", content="hi")])
        try:
            assert (await asyncio.create_task(anext(stream))).text == "first"
            advancing = asyncio.create_task(anext(stream))
            await waiting.wait()
            assert await coordinator.cancel("cross-task", owner_id="owner")
            with pytest.raises(asyncio.CancelledError):
                await advancing
            assert closed.is_set()
            assert (await coordinator.jobs())[0].state == "cancelled"
        finally:
            await stream.aclose()
            await model.close()


@pytest.mark.asyncio
async def test_provider_error_survives_failed_terminal_persistence() -> None:
    class FailingFinalStore(MemoryQueueStore):
        async def compare_and_set(
            self,
            call_id: str,
            version: int,
            state: QueueState,
            owner_id: str,
            *,
            provider_scope: str | None = None,
            attempt_id: str | None = None,
        ) -> CallJob | None:
            if state == "failed":
                raise QueueStateUnavailableError("persistence unavailable")
            return await super().compare_and_set(
                call_id,
                version,
                state,
                owner_id,
                provider_scope=provider_scope,
                attempt_id=attempt_id,
            )

    coordinator = CallQueueCoordinator(store=FailingFinalStore())
    with pytest.raises(RuntimeError, match="provider exploded"):
        async with coordinator.call(CallQueueContext("tenant", "owner")):
            raise RuntimeError("provider exploded")


@pytest.mark.asyncio
async def test_journal_rejects_ciphertext_transplant_and_version_tamper(tmp_path: Path) -> None:
    journal = QueueJournal(tmp_path / "calls.sqlite", RecordCipher(b"k" * 32), FakeAnchor())
    coordinator = CallQueueCoordinator(store=journal)
    await coordinator.initialize()
    await coordinator.register(CallQueueContext("a", "owner-a", "call-a"))
    await coordinator.register(CallQueueContext("b", "owner-b", "call-b"))
    with sqlite3.connect(tmp_path / "calls.sqlite") as db:
        rows = db.execute("SELECT id, sealed FROM jobs ORDER BY id").fetchall()
        db.execute("UPDATE jobs SET sealed = ? WHERE id = ?", (rows[0][1], rows[1][0]))
    with pytest.raises(QueueStateUnavailableError, match="rollback"):
        await journal.list_jobs()
    with sqlite3.connect(tmp_path / "calls.sqlite") as db:
        transplanted = db.execute(
            "SELECT id, version, updated, sealed FROM jobs WHERE id = ?", (rows[1][0],)
        ).fetchone()
    with pytest.raises(ValueError, match="binding"):
        journal._bound_job(transplanted)
    with sqlite3.connect(tmp_path / "calls.sqlite") as db:
        db.execute("UPDATE jobs SET version = 99 WHERE id = ?", (rows[0][0],))
    with sqlite3.connect(tmp_path / "calls.sqlite") as db:
        version_tampered = db.execute(
            "SELECT id, version, updated, sealed FROM jobs WHERE id = ?", (rows[0][0],)
        ).fetchone()
    with pytest.raises(ValueError, match="binding"):
        journal._bound_job(version_tampered)


@pytest.mark.asyncio
async def test_journal_controls_are_sealed_and_strict(tmp_path: Path) -> None:
    path = tmp_path / "calls.sqlite"
    journal = QueueJournal(path, RecordCipher(b"k" * 32), FakeAnchor())
    coordinator = CallQueueCoordinator(store=journal)
    await coordinator.initialize()
    await coordinator.pause()
    assert b"paused" not in path.read_bytes()
    restored = CallQueueCoordinator(store=journal)
    await restored.initialize()
    assert restored._paused
    with sqlite3.connect(path) as db:
        db.execute("UPDATE controls SET sealed = ?", ('{"paused": true}',))
    with pytest.raises(QueueStateUnavailableError, match="rollback"):
        await CallQueueCoordinator(store=journal).initialize()
    with pytest.raises(ValueError, match="not sealed"):
        journal._decode('{"paused": true}')
    with pytest.raises((ValueError, TypeError)):
        QueueLimits(max_concurrent=True)
    with pytest.raises((ValueError, TypeError)):
        QueueLimits(wait_timeout=float("nan"))


@pytest.mark.asyncio
async def test_journal_history_is_bounded_without_dropping_active_calls(tmp_path: Path) -> None:
    journal = QueueJournal(
        tmp_path / "calls.sqlite", RecordCipher(b"k" * 32), FakeAnchor(), history_limit=2
    )
    coordinator = CallQueueCoordinator(store=journal)
    await coordinator.initialize()
    first = await coordinator.register(CallQueueContext("a", "owner", "one"))
    await journal.compare_and_set(first.call_id, 0, "completed", "owner")
    await coordinator.register(CallQueueContext("a", "owner", "two"))
    await coordinator.register(CallQueueContext("a", "owner", "three"))
    assert await journal.get("one") is None
    with pytest.raises(QueueFullError):
        await coordinator.register(CallQueueContext("a", "owner", "four"))
    assert {job.call_id for job in await journal.list_jobs()} == {"two", "three"}


@pytest.mark.asyncio
async def test_crash_after_anchor_advance_replays_only_verified_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "calls.sqlite"
    cipher = RecordCipher(b"k" * 32)
    anchor = FakeAnchor()
    journal = QueueJournal(path, cipher, anchor)

    def crash_after_anchor(
        db: sqlite3.Connection,
        previous: AnchorHead,
        before: tuple[dict[str, tuple[int, float, str]], str | None],
    ) -> None:
        intent = journal._intent(before, journal._snapshot(db))
        anchor.compare_and_advance(previous, journal._digest(db), intent)
        raise RuntimeError("simulated process death before SQLite commit")

    monkeypatch.setattr(journal, "_commit_anchored", crash_after_anchor)
    crashing = CallQueueCoordinator(store=journal)
    await crashing.initialize()
    with pytest.raises(RuntimeError, match="simulated process death"):
        await crashing.register(CallQueueContext("tenant", "owner", "crashed-call"))
    recovered = QueueJournal(path, cipher, anchor)
    row = await recovered.get("crashed-call")
    assert row is not None and row.state == "queued"
    assert await CallQueueCoordinator(store=recovered).recover() == 1
    assert (await recovered.get("crashed-call")).state == "failed"


@pytest.mark.asyncio
async def test_older_database_replay_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "calls.sqlite"
    backup = tmp_path / "older.sqlite"
    cipher = RecordCipher(b"k" * 32)
    anchor = FakeAnchor()
    journal = QueueJournal(path, cipher, anchor)
    coordinator = CallQueueCoordinator(store=journal)
    await coordinator.initialize()
    await coordinator.register(CallQueueContext("tenant", "owner", "first"))
    source = sqlite3.connect(path)
    target = sqlite3.connect(backup)
    try:
        source.backup(target)
    finally:
        source.close()
        target.close()
    await coordinator.register(CallQueueContext("tenant", "owner", "second"))
    await coordinator.register(CallQueueContext("tenant", "owner", "third"))
    backup.chmod(0o600)
    Path(f"{path}-wal").unlink(missing_ok=True)
    Path(f"{path}-shm").unlink(missing_ok=True)
    os.replace(backup, path)
    with pytest.raises(QueueStateUnavailableError, match="rollback"):
        QueueJournal(path, cipher, anchor)


@pytest.mark.asyncio
async def test_granted_waiter_cancel_releases_reservation() -> None:
    coordinator = CallQueueCoordinator(limits=QueueLimits(max_concurrent=1))
    await coordinator.pause()

    async def pending() -> None:
        async with coordinator.call(CallQueueContext("tenant", "owner")) as job:
            async with coordinator.attempt("provider", job):
                pytest.fail("canceled waiter entered provider")

    task = asyncio.create_task(pending())
    await asyncio.sleep(0)
    pool = coordinator._pools["provider"]
    pool.grant_next(1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pool.active == 0
    await coordinator.resume()
    async with coordinator.call(CallQueueContext("tenant", "owner")) as job:
        async with coordinator.attempt("provider", job):
            assert pool.active == 1
    assert pool.active == 0


@pytest.mark.asyncio
async def test_canceled_head_waiter_does_not_block_following_tenant() -> None:
    pool = _ProviderPool()
    loop = asyncio.get_running_loop()
    canceled = _Waiter("a", loop.create_future())
    following = _Waiter("b", loop.create_future())
    pool.enqueue(canceled)
    pool.enqueue(following)
    canceled.ready.cancel()
    pool.grant_next(1)
    assert following.ready.done() and following.granted
    assert pool.active == 1


@pytest.mark.asyncio
async def test_resume_fills_all_available_slots_and_recovery_refuses_live_calls() -> None:
    coordinator = CallQueueCoordinator(limits=QueueLimits(max_concurrent=2))
    await coordinator.pause()
    entered = asyncio.Event()
    release = asyncio.Event()
    active = 0

    async def pending(owner: str) -> None:
        nonlocal active
        async with coordinator.call(CallQueueContext(owner, owner)) as job:
            async with coordinator.attempt("provider", job):
                active += 1
                if active == 2:
                    entered.set()
                await release.wait()

    one = asyncio.create_task(pending("a"))
    two = asyncio.create_task(pending("b"))
    await asyncio.sleep(0)
    await coordinator.resume()
    await asyncio.wait_for(entered.wait(), 1)
    with pytest.raises(RuntimeError, match="live"):
        await coordinator.recover()
    release.set()
    await asyncio.gather(one, two)


@pytest.mark.asyncio
async def test_stale_controller_cannot_overwrite_durable_pause(tmp_path: Path) -> None:
    journal = QueueJournal(tmp_path / "calls.sqlite", RecordCipher(b"k" * 32), FakeAnchor())
    first = CallQueueCoordinator(store=journal)
    second = CallQueueCoordinator(store=journal)
    await first.initialize()
    await second.initialize()
    await first.pause()
    with pytest.raises(QueueStateUnavailableError, match="stale"):
        await second.resume()
    assert second._control_revision == 0
    assert (await journal.load_control())["paused"] is True


@pytest.mark.asyncio
async def test_invalid_execution_budget_rejected_before_durable_accept(tmp_path: Path) -> None:
    journal = QueueJournal(tmp_path / "calls.sqlite", RecordCipher(b"k" * 32), FakeAnchor())
    coordinator = CallQueueCoordinator(store=journal)
    await coordinator.initialize()
    with pytest.raises(ValueError, match="execution_timeout"):
        async with coordinator.call(
            CallQueueContext("tenant", "owner"), execution_timeout=float("nan")
        ):
            pass
    assert await journal.list_jobs() == []


@pytest.mark.asyncio
async def test_anchor_outage_recovers_same_journal_without_process_restart(tmp_path: Path) -> None:
    anchor = FakeAnchor()
    journal = QueueJournal(tmp_path / "calls.sqlite", RecordCipher(b"k" * 32), anchor)
    coordinator = CallQueueCoordinator(store=journal)
    await coordinator.initialize()
    await coordinator.register(CallQueueContext("tenant", "owner", "call"))
    anchor.unavailable = True
    with pytest.raises(QueueStateUnavailableError, match="unavailable"):
        await journal.get("call")
    anchor.unavailable = False
    assert (await journal.get("call")).state == "queued"


@pytest.mark.asyncio
async def test_concurrent_journal_writers_and_readers_keep_anchor_coherent(tmp_path: Path) -> None:
    anchor = FakeAnchor()
    journal = QueueJournal(tmp_path / "calls.sqlite", RecordCipher(b"k" * 32), anchor)
    coordinator = CallQueueCoordinator(store=journal)
    await coordinator.initialize()

    async def writer(index: int) -> None:
        await coordinator.register(CallQueueContext("tenant", f"owner-{index}", f"call-{index}"))

    async def reader() -> None:
        for _ in range(15):
            await coordinator.jobs(tenant_id="tenant")
            await journal.load_control()

    await asyncio.gather(*(writer(index) for index in range(15)), reader(), reader())
    assert len(await coordinator.jobs()) == 15
    assert anchor.head is not None and anchor.head.version == 16


@pytest.mark.parametrize(
    ("config", "field"),
    [
        ({"max_concurrent": True}, "max_concurrent"),
        ({"max_concurrent": 1.5}, "max_concurrent"),
        ({"max_queued": False}, "max_queued"),
        ({"max_queued": 0.5}, "max_queued"),
        ({"call_timeout": float("nan")}, "call_timeout"),
        ({"call_timeout": float("inf")}, "call_timeout"),
        ({"call_timeout": True}, "call_timeout"),
    ],
)
def test_standalone_queue_rejects_malformed_limits(config: dict[str, object], field: str) -> None:
    with pytest.raises(ArcLLMConfigError, match=field):
        QueueModule(config, cast(LLMProvider, object()))
