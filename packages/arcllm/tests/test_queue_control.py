"""Shared queue ownership and durable state regressions."""

import asyncio
import hashlib
import json
import math
import os
import sqlite3
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from arctrust import AnchorHead, AnchorUnavailableError, RecordCipher, verify_signature
from nacl.signing import SigningKey

from arcllm import queue_merkle
from arcllm.exceptions import ArcLLMConfigError, QueueFullError, QueueStateUnavailableError
from arcllm.modules.queue import QueueModule
from arcllm.queue_control import (
    CallJob,
    CallQueueContext,
    CallQueueCoordinator,
    MemoryQueueStore,
    QueueLimits,
    QueueReadScope,
    QueueRecoveryPage,
    QueueState,
    _ProviderPool,
    _Waiter,
)


def test_optional_journal_absence_keeps_standalone_memory_queue_importable() -> None:
    code = (
        'import sys; sys.modules["arcllm.queue_journal"] = None; '
        "import arcllm, arcrun, arcagent; "
        "assert isinstance(arcllm.CallQueueCoordinator().store, arcllm.MemoryQueueStore)"
    )
    result = subprocess.run(  # noqa: S603 - fixed interpreter and test-only literal code
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_versioned_controls_reject_stale_revision_and_survive_restart(
    tmp_path: Path,
) -> None:
    journal = QueueJournal(tmp_path / "calls.sqlite", RecordCipher(b"k" * 32), FakeAnchor())
    first = CallQueueCoordinator(store=journal)
    second = CallQueueCoordinator(store=journal)
    await first.initialize()
    await second.initialize()
    paused = await first.pause(expected_revision=0)
    assert paused.revision == 1 and paused.paused
    with pytest.raises(QueueStateUnavailableError, match="stale"):
        await second.resume(expected_revision=0)
    restored = CallQueueCoordinator(store=journal)
    await restored.initialize()
    assert restored.control().revision == 1 and restored.control().paused


@pytest.mark.asyncio
async def test_peer_pause_prevents_provider_admission_until_peer_resume(tmp_path: Path) -> None:
    journal = QueueJournal(tmp_path / "calls.sqlite", RecordCipher(b"k" * 32), FakeAnchor())
    owner = CallQueueCoordinator(store=journal)
    controller = CallQueueCoordinator(store=journal)
    await owner.initialize()
    await controller.initialize()
    registered = asyncio.Event()
    proceed = asyncio.Event()
    entered = asyncio.Event()

    async def pending() -> None:
        async with owner.call(CallQueueContext("tenant", "owner", "peer-pause")) as job:
            registered.set()
            await proceed.wait()
            async with owner.attempt("provider", job):
                entered.set()

    task = asyncio.create_task(pending())
    await registered.wait()
    await controller.pause(expected_revision=0)
    proceed.set()
    await asyncio.sleep(0.2)
    assert not entered.is_set()
    queued = await journal.get("peer-pause")
    assert queued is not None and queued.state == "queued"
    await controller.resume(expected_revision=1)
    await asyncio.wait_for(task, 2)
    assert entered.is_set()


@pytest.mark.asyncio
async def test_cancel_queued_waiter_is_confirmed_and_never_enters_provider() -> None:
    coordinator = CallQueueCoordinator(limits=QueueLimits(max_concurrent=1))
    await coordinator.pause(expected_revision=0)
    entered = False

    async def pending() -> None:
        nonlocal entered
        async with coordinator.call(CallQueueContext("tenant", "owner", "queued")) as job:
            async with coordinator.attempt("provider", job):
                entered = True

    task = asyncio.create_task(pending())
    while await coordinator.store.get("queued") is None:
        await asyncio.sleep(0)
    job = await coordinator.store.get("queued")
    assert job is not None
    result = await coordinator.cancel("queued", owner_id="owner", expected_version=job.version)
    assert result.status == "confirmed"
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not entered
    assert (await coordinator.store.get("queued")).state == "cancelled"


@pytest.mark.asyncio
async def test_running_remote_cancel_is_requested_then_late_completion_is_known() -> None:
    store = MemoryQueueStore()
    first = CallQueueCoordinator(store=store)
    remote = CallQueueCoordinator(store=store)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def running() -> None:
        async with first.call(CallQueueContext("tenant", "owner", "running")) as job:
            async with first.attempt("provider", job):
                entered.set()
                await release.wait()

    task = asyncio.create_task(running())
    await entered.wait()
    job = await store.get("running")
    assert job is not None
    result = await remote.cancel("running", owner_id="owner", expected_version=job.version)
    assert result.status == "requested"
    assert (await store.get("running")).state == "cancel_requested"
    release.set()
    await task
    assert (await store.get("running")).state == "completed"


@pytest.mark.asyncio
async def test_provider_failure_after_remote_cancel_remains_failed() -> None:
    store = MemoryQueueStore()
    owner = CallQueueCoordinator(store=store)
    remote = CallQueueCoordinator(store=store)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def running() -> None:
        async with owner.call(CallQueueContext("tenant", "owner", "failing")) as job:
            async with owner.attempt("provider", job):
                entered.set()
                await release.wait()
                raise RuntimeError("provider failed")

    task = asyncio.create_task(running())
    await entered.wait()
    job = await store.get("failing")
    assert job is not None
    assert (
        await remote.cancel("failing", owner_id="owner", expected_version=job.version)
    ).status == "requested"
    release.set()
    with pytest.raises(RuntimeError, match="provider failed"):
        await task
    assert (await store.get("failing")).state == "failed"


@pytest.mark.asyncio
async def test_stale_job_version_does_not_cancel_or_disclose_foreign_owner() -> None:
    coordinator = CallQueueCoordinator()
    job = await coordinator.register(CallQueueContext("tenant", "owner", "call"))
    assert (
        await coordinator.cancel("call", owner_id="intruder", expected_version=0)
    ).status == "unavailable"
    assert (
        await coordinator.cancel("call", owner_id="owner", expected_version=1)
    ).status == "conflict"
    assert (await coordinator.store.get("call")).state == "queued"
    assert (
        await coordinator.cancel("call", owner_id="owner", expected_version=job.version)
    ).status == "confirmed"


@pytest.mark.asyncio
async def test_scoped_operator_cancel_never_mutates_foreign_tenant_or_owner() -> None:
    coordinator = CallQueueCoordinator()
    foreign = await coordinator.register(CallQueueContext("tenant-b", "owner-b", "foreign"))
    own = await coordinator.register(CallQueueContext("tenant-a", "owner-a", "own"))
    tenant_scope = QueueReadScope(tenant_id="tenant-a")
    assert (
        await coordinator.cancel_scoped(
            foreign.call_id, scope=tenant_scope, expected_version=foreign.version
        )
    ).status == "unavailable"
    assert (
        await coordinator.cancel_scoped(
            own.call_id,
            scope=QueueReadScope(tenant_id="tenant-a", owner_id="owner-b"),
            expected_version=own.version,
        )
    ).status == "unavailable"
    assert (await coordinator.store.get(foreign.call_id)).state == "queued"
    assert (await coordinator.store.get(own.call_id)).state == "queued"
    assert (
        await coordinator.cancel_scoped(own.call_id, scope=tenant_scope, expected_version=own.version)
    ).status == "confirmed"


@pytest.mark.asyncio
async def test_scoped_coordinator_rejects_foreign_admission() -> None:
    coordinator = CallQueueCoordinator(tenant_scope="tenant-a")
    with pytest.raises(ValueError, match="outside coordinator scope"):
        await coordinator.register(CallQueueContext("tenant-b", "owner", "foreign"))
    assert await coordinator.store.get("foreign") is None


@pytest.mark.asyncio
async def test_scoped_journal_refuses_authenticated_foreign_history(tmp_path: Path) -> None:
    anchor = FakeAnchor()
    path = tmp_path / "calls.sqlite"
    journal = QueueJournal(path, RecordCipher(b"k" * 32), anchor)
    coordinator = CallQueueCoordinator(store=journal)
    await coordinator.initialize()
    await coordinator.register(CallQueueContext("tenant-b", "owner", "foreign"))
    with pytest.raises(QueueStateUnavailableError, match="another tenant"):
        QueueJournal(path, RecordCipher(b"k" * 32), anchor, tenant_scope="tenant-a")


@pytest.mark.asyncio
async def test_running_cancel_blocks_new_provider_attempt() -> None:
    coordinator = CallQueueCoordinator()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def running() -> None:
        async with coordinator.call(CallQueueContext("tenant", "owner", "call")) as job:
            async with coordinator.attempt("first", job):
                entered.set()
                await release.wait()
            with pytest.raises(RuntimeError, match="fenced"):
                async with coordinator.attempt("second", job):
                    pytest.fail("cancelled call entered another provider")

    task = asyncio.create_task(running())
    await entered.wait()
    job = await coordinator.store.get("call")
    assert job is not None
    remote = CallQueueCoordinator(store=coordinator.store)
    assert (
        await remote.cancel("call", owner_id="owner", expected_version=job.version)
    ).status == "requested"
    release.set()
    await task
    assert (await coordinator.store.get("call")).state == "completed"


@pytest.mark.asyncio
async def test_durable_scoped_metadata_page_is_tenant_bounded(tmp_path: Path) -> None:
    journal = QueueJournal(tmp_path / "calls.sqlite", RecordCipher(b"k" * 32), FakeAnchor())
    coordinator = CallQueueCoordinator(store=journal)
    await coordinator.initialize()
    for index in range(41):
        tenant = "wanted" if index < 3 else "other"
        await coordinator.register(CallQueueContext(tenant, "owner", f"call-{index}"))
    with patch.object(journal, "_bound_job", wraps=journal._bound_job) as decoded:
        first = await coordinator.metadata_page(QueueReadScope("wanted"), limit=2)
    assert len(first.jobs) == 2 and first.next_cursor is not None
    assert decoded.call_count <= 3
    second = await coordinator.metadata_page(
        QueueReadScope("wanted"), cursor=first.next_cursor, limit=2
    )
    assert len(second.jobs) == 1 and second.next_cursor is None
    assert {job.call_id for job in (*first.jobs, *second.jobs)} == {
        "call-0",
        "call-1",
        "call-2",
    }
    with pytest.raises(ValueError, match="cursor"):
        await coordinator.metadata_page(QueueReadScope("other"), cursor=first.next_cursor, limit=2)


@pytest.mark.asyncio
async def test_durable_scoped_cursor_survives_restart_and_ignores_foreign_writes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "calls.sqlite"
    cipher = RecordCipher(b"k" * 32)
    anchor = FakeAnchor()
    journal = QueueJournal(path, cipher, anchor)
    first = CallQueueCoordinator(store=journal)
    await first.initialize()
    for index in range(3):
        await first.register(CallQueueContext("wanted", "owner", f"visible-{index}"))
    page = await first.metadata_page(QueueReadScope("wanted"), limit=1)
    assert page.next_cursor is not None
    await first.register(CallQueueContext("foreign", "owner", "foreign"))
    restored = CallQueueCoordinator(store=QueueJournal(path, cipher, anchor))
    await restored.initialize()
    following = await restored.metadata_page(
        QueueReadScope("wanted"), cursor=page.next_cursor, limit=2
    )
    assert len(following.jobs) == 2
    assert page.jobs[0].call_id not in {job.call_id for job in following.jobs}


@pytest.mark.asyncio
async def test_scoped_cursor_survives_foreign_branch_collapse(tmp_path: Path) -> None:
    journal = QueueJournal(
        tmp_path / "calls.sqlite", RecordCipher(b"k" * 32), FakeAnchor(), history_limit=4
    )
    coordinator = CallQueueCoordinator(store=journal)
    await coordinator.initialize()
    for index in range(3):
        await coordinator.register(CallQueueContext("wanted", "owner", f"wanted-{index}"))
    foreign = await coordinator.register(CallQueueContext("foreign", "owner", "foreign"))
    page = await coordinator.metadata_page(QueueReadScope("wanted"), limit=1)
    assert page.next_cursor is not None
    terminal = await journal.compare_and_set(foreign.call_id, foreign.version, "failed", "owner")
    assert terminal is not None
    await coordinator.register(CallQueueContext("another", "owner", "replacement"))
    assert await journal.get("foreign") is None
    following = await coordinator.metadata_page(
        QueueReadScope("wanted"), cursor=page.next_cursor, limit=2
    )
    assert len(following.jobs) == 2


@pytest.mark.asyncio
async def test_durable_scoped_page_rejects_index_omission(tmp_path: Path) -> None:
    path = tmp_path / "calls.sqlite"
    journal = QueueJournal(path, RecordCipher(b"k" * 32), FakeAnchor())
    coordinator = CallQueueCoordinator(store=journal)
    await coordinator.initialize()
    await coordinator.register(CallQueueContext("wanted", "owner", "visible"))
    with sqlite3.connect(path) as db:
        db.execute("UPDATE jobs SET tenant_key = ?", ("0" * 64,))
    with pytest.raises(QueueStateUnavailableError):
        await coordinator.metadata_page(QueueReadScope("wanted"), limit=2)


@pytest.mark.asyncio
async def test_durable_scoped_page_refuses_deleted_row_and_corrupt_proof(tmp_path: Path) -> None:
    path = tmp_path / "calls.sqlite"
    journal = QueueJournal(path, RecordCipher(b"k" * 32), FakeAnchor())
    coordinator = CallQueueCoordinator(store=journal)
    await coordinator.initialize()
    await coordinator.register(CallQueueContext("wanted", "owner", "visible"))
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM jobs")
    with pytest.raises(QueueStateUnavailableError):
        await coordinator.metadata_page(QueueReadScope("wanted"), limit=2)

    separate = tmp_path / "separate.sqlite"
    clean = QueueJournal(separate, RecordCipher(b"k" * 32), FakeAnchor())
    valid = CallQueueCoordinator(store=clean)
    await valid.initialize()
    await valid.register(CallQueueContext("wanted", "owner", "visible"))
    with sqlite3.connect(separate) as db:
        scope = queue_merkle.scope_key(queue_merkle.tenant_key("wanted"), None, None)
        db.execute(
            "UPDATE queue_nodes SET digest = ? WHERE prefix LIKE ? AND kind = 'leaf'",
            ("0" * 64, scope + "%"),
        )
    with pytest.raises(QueueStateUnavailableError):
        await valid.metadata_page(QueueReadScope("wanted"), limit=2)


@pytest.mark.asyncio
async def test_compressed_branch_rejects_omitted_or_extra_child(tmp_path: Path) -> None:
    for index, replacement in enumerate(("[]", '[["f", "' + "0" * 64 + '"]]')):
        path = tmp_path / f"branch-{index}.sqlite"
        journal = QueueJournal(path, RecordCipher(b"k" * 32), FakeAnchor())
        coordinator = CallQueueCoordinator(store=journal)
        await coordinator.initialize()
        await coordinator.register(CallQueueContext("tenant", "owner", "visible"))
        with sqlite3.connect(path) as db:
            prefix = db.execute(
                "SELECT prefix FROM queue_nodes WHERE kind = 'branch' "
                "ORDER BY length(prefix), prefix LIMIT 1"
            ).fetchone()[0]
            db.execute(
                "UPDATE queue_nodes SET children = ? WHERE prefix = ?", (replacement, prefix)
            )
        with pytest.raises(QueueStateUnavailableError):
            await coordinator.metadata_page(QueueReadScope("tenant"), limit=2)


@pytest.mark.asyncio
async def test_durable_scoped_page_filters_owner_and_state_without_foreign_cursor(
    tmp_path: Path,
) -> None:
    journal = QueueJournal(tmp_path / "calls.sqlite", RecordCipher(b"k" * 32), FakeAnchor())
    coordinator = CallQueueCoordinator(store=journal)
    await coordinator.initialize()
    one = await coordinator.register(CallQueueContext("tenant", "owner-a", "one"))
    await coordinator.register(CallQueueContext("tenant", "owner-b", "two"))
    await coordinator.register(CallQueueContext("tenant", "owner-a", "three"))
    await journal.compare_and_set(one.call_id, one.version, "completed", "owner-a")
    page = await coordinator.metadata_page(QueueReadScope("tenant", "owner-a", "queued"), limit=1)
    assert [job.call_id for job in page.jobs] == ["three"]
    assert page.next_cursor is None
    completed = await coordinator.metadata_page(
        QueueReadScope("tenant", "owner-a", "completed"), limit=1
    )
    assert [job.call_id for job in completed.jobs] == ["one"]


def test_journal_refuses_old_format_without_rewriting_it(tmp_path: Path) -> None:
    path = tmp_path / "calls.sqlite"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE jobs (id TEXT PRIMARY KEY, version INTEGER, updated REAL, sealed TEXT)"
        )
    path.chmod(0o600)
    with pytest.raises(QueueStateUnavailableError, match="format"):
        QueueJournal(path, RecordCipher(b"k" * 32), FakeAnchor())
    with sqlite3.connect(path) as db:
        assert [row[1] for row in db.execute("PRAGMA table_info(jobs)")] == [
            "id",
            "version",
            "updated",
            "sealed",
        ]
        assert {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        } == {"jobs"}


def test_journal_refuses_format_three_without_rewriting_it(tmp_path: Path) -> None:
    path = tmp_path / "calls.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE queue_meta (id INTEGER PRIMARY KEY, format INTEGER NOT NULL)")
        db.execute("INSERT INTO queue_meta(id, format) VALUES (1, 3)")
    path.chmod(0o600)
    with pytest.raises(QueueStateUnavailableError, match="format"):
        QueueJournal(path, RecordCipher(b"k" * 32), FakeAnchor())
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT format FROM queue_meta WHERE id = 1").fetchone() == (3,)
        assert {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        } == {"queue_meta"}


@pytest.mark.parametrize("value", [-1.0, math.nan, math.inf, -math.inf])
def test_queue_index_rejects_invalid_timestamp(value: float) -> None:
    with pytest.raises(ValueError, match="timestamp"):
        queue_merkle.sort_suffix(value, "0" * 64)


def test_recovery_page_contract_is_public_through_adjacent_facade() -> None:
    import arcllm

    assert arcllm.QueueRecoveryPage is QueueRecoveryPage


@pytest.mark.asyncio
async def test_durable_page_proof_cost_is_page_bounded_with_large_tenant(tmp_path: Path) -> None:
    path = tmp_path / "calls.sqlite"
    journal = QueueJournal(path, RecordCipher(b"k" * 32), FakeAnchor(), history_limit=200)
    coordinator = CallQueueCoordinator(store=journal)
    await coordinator.initialize()
    for index in range(150):
        await coordinator.register(CallQueueContext("tenant", "owner", f"call-{index}"))
    queries: list[str] = []
    with sqlite3.connect(path) as db:
        db.set_trace_callback(queries.append)
        scope = queue_merkle.scope_key(queue_merkle.tenant_key("tenant"), None, None)
        first = queue_merkle.page_keys(
            db, scope, journal._anchor.latest().digest, after=None, limit=2
        )
        assert len(first) == 2
        assert [queue_merkle.row_for_key(db, key)[0] for key in first]
    assert len(queries) < 400
    assert not any(
        "SELECT id, tenant_key" in query and "FROM jobs WHERE id" not in query for query in queries
    )
    page = await coordinator.metadata_page(QueueReadScope("tenant"), limit=100)
    assert len(page.jobs) == 100 and page.next_cursor is not None
    following = await coordinator.metadata_page(
        QueueReadScope("tenant"), cursor=page.next_cursor, limit=100
    )
    assert len(following.jobs) == 50 and following.next_cursor is None


@pytest.mark.asyncio
async def test_metadata_page_does_not_reveal_foreign_activity() -> None:
    store = MemoryQueueStore()
    coordinator = CallQueueCoordinator(store=store)
    await coordinator.register(CallQueueContext("tenant", "owner", "visible"))
    baseline = await coordinator.metadata_page(QueueReadScope("tenant"), limit=2)
    assert [job.call_id for job in baseline.jobs] == ["visible"]
    assert baseline.next_cursor is None
    for index in range(4):
        await coordinator.register(CallQueueContext("foreign", "owner", f"foreign-{index}"))
    after = await coordinator.metadata_page(QueueReadScope("tenant"), limit=2)
    assert after == baseline


@pytest.mark.asyncio
async def test_memory_metadata_cursor_rejects_another_scope() -> None:
    store = MemoryQueueStore()
    first = CallQueueCoordinator(store=store)
    for index in range(8):
        await first.register(CallQueueContext("tenant", "owner", f"call-{index}"))
    page = await first.metadata_page(QueueReadScope("tenant"), limit=2)
    assert len(page.jobs) == 2 and page.next_cursor is not None
    restored = CallQueueCoordinator(store=store)
    following = await restored.metadata_page(
        QueueReadScope("tenant"), cursor=page.next_cursor, limit=2
    )
    assert len(following.jobs) == 2
    assert not {job.call_id for job in page.jobs} & {job.call_id for job in following.jobs}
    with pytest.raises(ValueError, match="cursor"):
        await restored.metadata_page(QueueReadScope("other"), cursor=page.next_cursor, limit=2)


@pytest.mark.asyncio
async def test_configure_returns_effective_revision_and_rejects_stale_write() -> None:
    coordinator = CallQueueCoordinator()
    limits = QueueLimits(max_concurrent=3, max_queued=2)
    changed = await coordinator.configure(limits, expected_revision=0)
    assert changed.revision == 1 and changed.limits == limits
    with pytest.raises(QueueStateUnavailableError, match="stale"):
        await coordinator.configure(QueueLimits(max_concurrent=1), expected_revision=0)
    assert coordinator.control() == changed


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


class FakeRecoveryAuthority:
    """Test broker that checks a signed lease in the same queue-root CAS."""

    def __init__(self, anchor: FakeAnchor) -> None:
        self.anchor = anchor
        self.signer = SigningKey.generate()
        self.revoked = False
        self.expired = False
        self.lose_next = False
        self.revoke_after_tenant: str | None = None

    def issue(self, tenant_id: str, owner_epoch: str, *, scope: str | None = None) -> str:
        body = json.dumps(
            [scope or self.anchor.scope, tenant_id, owner_epoch, "queue.recover"],
            separators=(",", ":"),
        ).encode()
        return f"{body.hex()}.{self.signer.sign(body).signature.hex()}"

    def validate(
        self,
        proof: str,
        *,
        journal_scope: str,
        tenant_id: str,
        owner_epoch: str,
        purpose: str,
    ) -> None:
        try:
            body_hex, signature_hex = proof.split(".", 1)
            body = bytes.fromhex(body_hex)
            signature = bytes.fromhex(signature_hex)
            fields = json.loads(body)
        except (ValueError, TypeError) as exc:
            raise AnchorUnavailableError("invalid recovery proof") from exc
        if (
            self.revoked
            or self.expired
            or fields != [journal_scope, tenant_id, owner_epoch, purpose]
            or journal_scope != self.anchor.scope
            or not verify_signature("ed25519", body, signature, bytes(self.signer.verify_key))
        ):
            raise AnchorUnavailableError("recovery proof expired, revoked, or wrong scope")

    def compare_and_advance(
        self,
        expected: AnchorHead,
        digest: str,
        intent: str,
        proof: str,
        *,
        journal_scope: str,
        tenant_id: str,
        owner_epoch: str,
        purpose: str,
    ) -> AnchorHead:
        self.validate(
            proof,
            journal_scope=journal_scope,
            tenant_id=tenant_id,
            owner_epoch=owner_epoch,
            purpose=purpose,
        )
        head = self.anchor.compare_and_advance(expected, digest, intent)
        if tenant_id == self.revoke_after_tenant:
            self.revoked = True
            self.revoke_after_tenant = None
        if self.lose_next:
            self.lose_next = False
            raise AnchorUnavailableError("recovery anchor response lost")
        return head


class LostResponseAnchor(FakeAnchor):
    """Advance durable authority, then lose the caller's response once."""

    def __init__(self) -> None:
        super().__init__()
        self.lose_next = False

    def compare_and_advance(
        self, expected: AnchorHead | None, digest: str, intent: str
    ) -> AnchorHead:
        head = super().compare_and_advance(expected, digest, intent)
        if self.lose_next:
            self.lose_next = False
            raise AnchorUnavailableError("anchor response lost")
        return head


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
            assert b'"tenant"' not in raw and b"provider:primary" not in raw
    row = await journal.get("call-1")
    assert row is not None and row.state == "completed"


@pytest.mark.asyncio
async def test_recovery_never_replays_or_accepts_stale_owner(tmp_path: Path) -> None:
    anchor = FakeAnchor()
    authority = FakeRecoveryAuthority(anchor)
    journal = QueueJournal(
        tmp_path / "calls.sqlite",
        RecordCipher(b"k" * 32),
        anchor,
        recovery_authority=authority,
    )
    coordinator = CallQueueCoordinator(store=journal)
    await coordinator.initialize()
    context = CallQueueContext(tenant_id="tenant", owner_id="run", call_id="call-2")
    job = await coordinator.register(context)
    started = await journal.compare_and_set(job.call_id, job.version, "running", "run")
    assert started is not None
    recovered = await CallQueueCoordinator(store=journal).recover(
        owned_epoch="run", recovery_proofs={"tenant": authority.issue("tenant", "run")}
    )
    assert recovered == 1
    row = await journal.get(job.call_id)
    assert row is not None and row.state == "outcome_unknown"
    assert await journal.compare_and_set(job.call_id, started.version, "completed", "run") is None


@pytest.mark.asyncio
async def test_durable_recovery_rejects_forged_foreign_expired_and_revoked_fence(
    tmp_path: Path,
) -> None:
    anchor = FakeAnchor()
    authority = FakeRecoveryAuthority(anchor)
    journal = QueueJournal(
        tmp_path / "calls.sqlite",
        RecordCipher(b"k" * 32),
        anchor,
        recovery_authority=authority,
    )
    coordinator = CallQueueCoordinator(store=journal)
    await coordinator.initialize()
    job = await coordinator.register(CallQueueContext("tenant", "epoch", "call"))
    invalid = [
        "forged",
        authority.issue("foreign", "epoch"),
        authority.issue("tenant", "other-epoch"),
        authority.issue("tenant", "epoch", scope="other/journal"),
    ]
    for proof in invalid:
        with pytest.raises(QueueStateUnavailableError):
            await journal.recovery_transition_batch((job,), owned_epoch="epoch", proof=proof)
        assert (await journal.get("call")).state == "queued"
    valid = authority.issue("tenant", "epoch")
    authority.expired = True
    with pytest.raises(QueueStateUnavailableError):
        await journal.recovery_transition_batch((job,), owned_epoch="epoch", proof=valid)
    authority.expired = False
    authority.revoked = True
    with pytest.raises(QueueStateUnavailableError):
        await journal.recovery_transition_batch((job,), owned_epoch="epoch", proof=valid)
    authority.revoked = False
    assert await journal.recovery_transition_batch((job,), owned_epoch="epoch", proof=valid) == 1


@pytest.mark.asyncio
async def test_recovery_requires_every_selected_tenant_proof_before_first_batch(
    tmp_path: Path,
) -> None:
    anchor = FakeAnchor()
    authority = FakeRecoveryAuthority(anchor)
    journal = QueueJournal(
        tmp_path / "calls.sqlite",
        RecordCipher(b"k" * 32),
        anchor,
        recovery_authority=authority,
    )
    coordinator = CallQueueCoordinator(store=journal)
    await coordinator.initialize()
    for tenant, count in (("a", 3), ("b", 2)):
        for index in range(count):
            await coordinator.register(CallQueueContext(tenant, "epoch", f"{tenant}-{index}"))
    baseline = anchor.head.version
    with pytest.raises(QueueStateUnavailableError, match="tenant proof"):
        await coordinator.recover(
            owned_epoch="epoch", recovery_proofs={"a": authority.issue("a", "epoch")}
        )
    assert anchor.head.version == baseline
    assert (await journal.get("a-0")).state == "queued"
    with pytest.raises(QueueStateUnavailableError, match="proof refused"):
        await coordinator.recover(
            owned_epoch="epoch",
            recovery_proofs={"a": authority.issue("a", "epoch"), "b": "forged"},
        )
    assert anchor.head.version == baseline
    assert (
        await coordinator.recover(
            owned_epoch="epoch",
            recovery_proofs={
                "a": authority.issue("a", "epoch"),
                "b": authority.issue("b", "epoch"),
            },
        )
        == 5
    )
    assert anchor.head.version == baseline + 2


@pytest.mark.asyncio
async def test_recovery_revocation_after_first_tenant_is_safe_and_resumable(
    tmp_path: Path,
) -> None:
    anchor = FakeAnchor()
    authority = FakeRecoveryAuthority(anchor)
    journal = QueueJournal(
        tmp_path / "calls.sqlite",
        RecordCipher(b"k" * 32),
        anchor,
        recovery_authority=authority,
    )
    coordinator = CallQueueCoordinator(store=journal)
    await coordinator.initialize()
    await coordinator.register(CallQueueContext("a", "epoch", "a-job"))
    await coordinator.register(CallQueueContext("b", "epoch", "b-job"))
    authority.revoke_after_tenant = "a"
    with pytest.raises(QueueStateUnavailableError):
        await coordinator.recover(
            owned_epoch="epoch",
            recovery_proofs={
                "a": authority.issue("a", "epoch"),
                "b": authority.issue("b", "epoch"),
            },
        )
    assert (await journal.get("a-job")).state == "failed"
    assert (await journal.get("b-job")).state == "queued"
    authority.revoked = False
    assert (
        await coordinator.recover(
            owned_epoch="epoch", recovery_proofs={"b": authority.issue("b", "epoch")}
        )
        == 1
    )
    assert (await journal.get("b-job")).state == "failed"


@pytest.mark.asyncio
async def test_recovery_batch_response_loss_replays_exact_authorized_intent(
    tmp_path: Path,
) -> None:
    anchor = FakeAnchor()
    authority = FakeRecoveryAuthority(anchor)
    path = tmp_path / "calls.sqlite"
    cipher = RecordCipher(b"k" * 32)
    journal = QueueJournal(path, cipher, anchor, recovery_authority=authority)
    coordinator = CallQueueCoordinator(store=journal)
    await coordinator.initialize()
    first = await coordinator.register(CallQueueContext("tenant", "epoch", "first"))
    second = await coordinator.register(CallQueueContext("tenant", "epoch", "second"))
    authority.lose_next = True
    with pytest.raises(QueueStateUnavailableError):
        await journal.recovery_transition_batch(
            (first, second), owned_epoch="epoch", proof=authority.issue("tenant", "epoch")
        )
    reopened = QueueJournal(path, cipher, anchor, recovery_authority=authority)
    assert (await reopened.get("first")).state == "failed"
    assert (await reopened.get("second")).state == "failed"


@pytest.mark.asyncio
async def test_recovery_marks_orphaned_cancel_request_unknown(tmp_path: Path) -> None:
    anchor = FakeAnchor()
    authority = FakeRecoveryAuthority(anchor)
    journal = QueueJournal(
        tmp_path / "calls.sqlite",
        RecordCipher(b"k" * 32),
        anchor,
        recovery_authority=authority,
    )
    owner = CallQueueCoordinator(store=journal)
    await owner.initialize()
    job = await owner.register(CallQueueContext("tenant", "run", "orphan-cancel"))
    running = await journal.compare_and_set(job.call_id, job.version, "running", "run")
    assert running is not None
    cancelled = await journal.compare_and_set(
        job.call_id, running.version, "cancel_requested", "run"
    )
    assert cancelled is not None
    assert (
        await CallQueueCoordinator(store=journal).recover(
            owned_epoch="run", recovery_proofs={"tenant": authority.issue("tenant", "run")}
        )
        == 1
    )
    final = await journal.get(job.call_id)
    assert final is not None and final.state == "outcome_unknown"


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
            assert (
                await coordinator.cancel(
                    "stream-call",
                    owner_id="owner",
                    expected_version=(await coordinator.store.get("stream-call")).version,
                )
            ).status == "requested"
            assert closed.is_set()
            assert (await coordinator.jobs())[0].state == "cancel_requested"
            await asyncio.create_task(stream.aclose())
            assert (await coordinator.jobs())[0].state == "cancelled"
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
            assert (
                await coordinator.cancel(
                    "idle-stream",
                    owner_id="owner",
                    expected_version=(await coordinator.store.get("idle-stream")).version,
                )
            ).status == "requested"
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
            assert (
                await coordinator.cancel(
                    "cross-task",
                    owner_id="owner",
                    expected_version=(await coordinator.store.get("cross-task")).version,
                )
            ).status == "requested"
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
    with pytest.raises(QueueStateUnavailableError, match="proof unavailable"):
        await journal.list_jobs()
    with sqlite3.connect(tmp_path / "calls.sqlite") as db:
        transplanted = db.execute(
            "SELECT id, tenant_key, owner_key, state, version, updated, sealed "
            "FROM jobs WHERE id = ?",
            (rows[1][0],),
        ).fetchone()
    with pytest.raises(ValueError, match="binding"):
        journal._bound_job(transplanted)
    with sqlite3.connect(tmp_path / "calls.sqlite") as db:
        db.execute("UPDATE jobs SET version = 99 WHERE id = ?", (rows[0][0],))
    with sqlite3.connect(tmp_path / "calls.sqlite") as db:
        version_tampered = db.execute(
            "SELECT id, tenant_key, owner_key, state, version, updated, sealed "
            "FROM jobs WHERE id = ?",
            (rows[0][0],),
        ).fetchone()
    with pytest.raises(ValueError, match="binding"):
        journal._bound_job(version_tampered)


@pytest.mark.asyncio
async def test_journal_controls_are_sealed_and_strict(tmp_path: Path) -> None:
    path = tmp_path / "calls.sqlite"
    journal = QueueJournal(path, RecordCipher(b"k" * 32), FakeAnchor())
    coordinator = CallQueueCoordinator(store=journal)
    await coordinator.initialize()
    await coordinator.pause(expected_revision=coordinator.control().revision)
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
        before: tuple[dict[str, tuple[str, str, str, int, float, str]], str | None],
        after: tuple[dict[str, tuple[str, str, str, int, float, str]], str | None],
    ) -> None:
        intent = journal._intent(before, after)
        journal._update_indexes(db, before, after)
        anchor.compare_and_advance(previous, journal._digest(db), intent)
        raise RuntimeError("simulated process death before SQLite commit")

    monkeypatch.setattr(journal, "_commit_anchored", crash_after_anchor)
    crashing = CallQueueCoordinator(store=journal)
    await crashing.initialize()
    with pytest.raises(RuntimeError, match="simulated process death"):
        await crashing.register(CallQueueContext("tenant", "owner", "crashed-call"))
    authority = FakeRecoveryAuthority(anchor)
    recovered = QueueJournal(path, cipher, anchor, recovery_authority=authority)
    row = await recovered.get("crashed-call")
    assert row is not None and row.state == "queued"
    assert (
        await CallQueueCoordinator(store=recovered).recover(
            owned_epoch="owner", recovery_proofs={"tenant": authority.issue("tenant", "owner")}
        )
        == 1
    )
    assert (await recovered.get("crashed-call")).state == "failed"


@pytest.mark.asyncio
async def test_lost_anchor_response_recovers_create_and_eviction(tmp_path: Path) -> None:
    path = tmp_path / "calls.sqlite"
    cipher = RecordCipher(b"k" * 32)
    anchor = LostResponseAnchor()
    journal = QueueJournal(path, cipher, anchor, history_limit=2)
    coordinator = CallQueueCoordinator(store=journal)
    await coordinator.initialize()
    first = await coordinator.register(CallQueueContext("tenant", "owner", "first"))
    await journal.compare_and_set(first.call_id, first.version, "completed", "owner")
    await coordinator.register(CallQueueContext("tenant", "owner", "second"))
    anchor.lose_next = True
    with pytest.raises(QueueStateUnavailableError):
        await coordinator.register(CallQueueContext("tenant", "owner", "third"))
    restored = QueueJournal(path, cipher, anchor, history_limit=2)
    assert await restored.get("first") is None
    assert {job.call_id for job in await restored.list_jobs()} == {"second", "third"}


@pytest.mark.asyncio
async def test_lost_anchor_response_recovers_state_and_control(tmp_path: Path) -> None:
    path = tmp_path / "calls.sqlite"
    cipher = RecordCipher(b"k" * 32)
    anchor = LostResponseAnchor()
    journal = QueueJournal(path, cipher, anchor)
    coordinator = CallQueueCoordinator(store=journal)
    await coordinator.initialize()
    job = await coordinator.register(CallQueueContext("tenant", "owner", "call"))
    anchor.lose_next = True
    with pytest.raises(QueueStateUnavailableError):
        await journal.compare_and_set(job.call_id, job.version, "running", "owner")
    recovered = QueueJournal(path, cipher, anchor)
    assert (await recovered.get("call")).state == "running"
    anchor.lose_next = True
    with pytest.raises(QueueStateUnavailableError):
        await recovered.save_control({"paused": True}, expected_revision=0)
    final = QueueJournal(path, cipher, anchor)
    assert (await final.load_control()) == {"paused": True, "revision": 1}


@pytest.mark.asyncio
async def test_recovery_snapshot_fences_epoch_and_excludes_new_jobs(tmp_path: Path) -> None:
    anchor = FakeAnchor()
    authority = FakeRecoveryAuthority(anchor)
    journal = QueueJournal(
        tmp_path / "calls.sqlite",
        RecordCipher(b"k" * 32),
        anchor,
        history_limit=200,
        recovery_authority=authority,
    )
    coordinator = CallQueueCoordinator(store=journal)
    await coordinator.initialize()
    for index in range(120):
        owner = "epoch" if index % 2 == 0 else "epoch-other"
        await journal.create(
            CallJob(
                call_id=f"job-{index}",
                tenant_id="tenant",
                owner_id=owner,
                agent_id=None,
                session_id=None,
                run_id=None,
                state="queued",
                version=0,
                created_at=float(1000 + index),
                updated_at=float(2000 + (119 - index)),
            )
        )
    original_page = journal.recovery_page
    inserted = False

    async def page_with_new_job(*, cursor: str | None, limit: int) -> object:
        nonlocal inserted
        page = await original_page(cursor=cursor, limit=limit)
        if not inserted:
            inserted = True
            await journal.create(
                CallJob(
                    call_id="new-after-snapshot",
                    tenant_id="tenant",
                    owner_id="epoch",
                    agent_id=None,
                    session_id=None,
                    run_id=None,
                    state="queued",
                    version=0,
                    created_at=9000.0,
                    updated_at=9000.0,
                )
            )
        return page

    with patch.object(journal, "recovery_page", side_effect=page_with_new_job):
        assert (
            await coordinator.recover(
                owned_epoch="epoch", recovery_proofs={"tenant": authority.issue("tenant", "epoch")}
            )
            == 60
        )
    assert (await journal.get("new-after-snapshot")).state == "queued"
    for index in range(120):
        row = await journal.get(f"job-{index}")
        assert row is not None
        assert row.state == ("failed" if index % 2 == 0 else "queued")
    with pytest.raises(ValueError, match="fenced owner epoch"):
        await coordinator.recover()
    with pytest.raises(ValueError, match="fenced owner epoch"):
        await coordinator.recover(owned_epoch="")
    with pytest.raises(ValueError, match="fenced owner epoch"):
        await coordinator.recover(owned_epoch="epoch:")


@pytest.mark.asyncio
async def test_recovery_page_refuses_corrupted_remaining_job(tmp_path: Path) -> None:
    path = tmp_path / "calls.sqlite"
    journal = QueueJournal(path, RecordCipher(b"k" * 32), FakeAnchor())
    await journal.create(
        CallJob(
            call_id="a",
            tenant_id="tenant",
            owner_id="epoch",
            agent_id=None,
            session_id=None,
            run_id=None,
            state="queued",
            version=0,
            created_at=1.0,
            updated_at=1.0,
        )
    )
    await journal.create(
        CallJob(
            call_id="b",
            tenant_id="tenant",
            owner_id="epoch",
            agent_id=None,
            session_id=None,
            run_id=None,
            state="queued",
            version=0,
            created_at=2.0,
            updated_at=2.0,
        )
    )
    first = await journal.recovery_page(cursor=None, limit=1)
    assert first.next_cursor is not None
    remaining = ({"a", "b"} - {first.jobs[0].call_id}).pop()
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE jobs SET sealed = ? WHERE id = ?",
            (
                "corrupt",
                hashlib.sha256(remaining.encode()).hexdigest(),
            ),
        )
    with pytest.raises(QueueStateUnavailableError):
        await journal.recovery_page(cursor=first.next_cursor, limit=1)


@pytest.mark.asyncio
async def test_recovery_page_allows_only_proven_terminal_retention(tmp_path: Path) -> None:
    journal = QueueJournal(
        tmp_path / "calls.sqlite", RecordCipher(b"k" * 32), FakeAnchor(), history_limit=2
    )
    first_id, terminal_id = sorted(
        ("first", "terminal"), key=lambda value: hashlib.sha256(value.encode()).hexdigest()
    )
    for index, call_id in enumerate((first_id, terminal_id)):
        await journal.create(
            CallJob(
                call_id=call_id,
                tenant_id="tenant",
                owner_id="epoch",
                agent_id=None,
                session_id=None,
                run_id=None,
                state="queued",
                version=0,
                created_at=float(index + 1),
                updated_at=float(index + 1),
            )
        )
    await journal.compare_and_set(terminal_id, 0, "completed", "epoch")
    first = await journal.recovery_page(cursor=None, limit=1)
    assert first.jobs[0].call_id == first_id and first.next_cursor is not None
    await journal.create(
        CallJob(
            call_id="new",
            tenant_id="tenant",
            owner_id="epoch",
            agent_id=None,
            session_id=None,
            run_id=None,
            state="queued",
            version=0,
            created_at=3.0,
            updated_at=3.0,
        )
    )
    second = await journal.recovery_page(cursor=first.next_cursor, limit=1)
    assert second.jobs == () and second.next_cursor is None


@pytest.mark.asyncio
async def test_recovery_cas_race_keeps_known_completion(tmp_path: Path) -> None:
    anchor = FakeAnchor()
    authority = FakeRecoveryAuthority(anchor)
    journal = QueueJournal(
        tmp_path / "calls.sqlite",
        RecordCipher(b"k" * 32),
        anchor,
        recovery_authority=authority,
    )
    coordinator = CallQueueCoordinator(store=journal)
    await coordinator.initialize()
    await coordinator.register(CallQueueContext("tenant", "epoch", "call"))
    original = journal.recovery_transition_batch
    raced = False

    async def concurrent_completion(*args: object, **kwargs: object) -> int:
        nonlocal raced
        if not raced:
            raced = True
            await journal.compare_and_set("call", 0, "completed", "epoch")
        return await original(*args, **kwargs)

    with patch.object(journal, "recovery_transition_batch", side_effect=concurrent_completion):
        assert (
            await coordinator.recover(
                owned_epoch="epoch", recovery_proofs={"tenant": authority.issue("tenant", "epoch")}
            )
            == 0
        )
    assert (await journal.get("call")).state == "completed"


@pytest.mark.asyncio
async def test_durable_cursor_refuses_changed_scope_after_state_transition(tmp_path: Path) -> None:
    journal = QueueJournal(tmp_path / "calls.sqlite", RecordCipher(b"k" * 32), FakeAnchor())
    coordinator = CallQueueCoordinator(store=journal)
    await coordinator.initialize()
    for index in range(3):
        await coordinator.register(CallQueueContext("tenant", "owner", f"call-{index}"))
    first = await coordinator.metadata_page(QueueReadScope("tenant"), limit=1)
    assert first.next_cursor is not None
    await journal.compare_and_set("call-0", 0, "completed", "owner")
    with pytest.raises(ValueError, match="stale queue cursor"):
        await coordinator.metadata_page(
            QueueReadScope("tenant"), cursor=first.next_cursor, limit=1
        )


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
    await coordinator.pause(expected_revision=coordinator.control().revision)

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
    await coordinator.resume(expected_revision=coordinator.control().revision)
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
    await coordinator.pause(expected_revision=coordinator.control().revision)
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
    await coordinator.resume(expected_revision=coordinator.control().revision)
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
    await first.pause(expected_revision=first.control().revision)
    with pytest.raises(QueueStateUnavailableError, match="stale"):
        await second.resume(expected_revision=second.control().revision)
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
