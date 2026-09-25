"""The hosted queue factory composes one initialized, scoped coordinator."""

from pathlib import Path
from typing import Any, cast

import arcrun
import arctrust
import pytest

from arccli.queue_runtime import open_durable_queue


class _Anchor:
    scope = "queue/tenant-a"
    tenant_id = "tenant-a"
    owner_epoch = "2"
    fenced_through_epoch = "1"

    def __init__(self) -> None:
        self.head: arctrust.AnchorHead | None = None
        self.record_calls: list[str] = []

    def seal_record(self, payload: bytes) -> str:
        self.record_calls.append("seal")
        return payload.hex()

    def open_record(self, sealed: str) -> bytes:
        self.record_calls.append("open")
        return bytes.fromhex(sealed)

    def latest(self) -> arctrust.AnchorHead | None:
        return self.head

    def compare_and_advance(
        self,
        expected: arctrust.AnchorHead | None,
        digest: str,
        intent: str,
        *args: Any,
        **kwargs: Any,
    ) -> arctrust.AnchorHead:
        assert expected == self.head
        self.head = arctrust.AnchorHead(
            scope=self.scope,
            version=(expected.version + 1 if expected else 1),
            digest=digest,
            previous_digest=(expected.digest if expected else None),
            intent=intent,
        )
        return self.head

    def recovery_proof(self, prior_owner_epoch: str) -> str:
        assert prior_owner_epoch == "1"
        return "signed-proof"

    def validate(self, *_args: Any, **_kwargs: Any) -> None:
        pass


@pytest.mark.asyncio
async def test_factory_opens_one_durable_scoped_queue(tmp_path: Path) -> None:
    raw_anchor = _Anchor()
    anchor = cast(arctrust.QueueBrokerAnchor, raw_anchor)
    runtime = await open_durable_queue(
        anchor,
        journal_path=tmp_path / "queue" / "journal.sqlite",
    )
    assert runtime.tenant_id == "tenant-a"
    assert runtime.owner_epoch == "2"
    assert runtime.coordinator.tenant_scope == "tenant-a"
    assert runtime.coordinator.store.tenant_scope == "tenant-a"
    assert runtime.recovered == 0
    await runtime.coordinator.store.create(
        arcrun.CallJob(
            call_id="new-call",
            tenant_id="tenant-a",
            owner_id="2:run-a",
            agent_id=None,
            session_id=None,
            run_id="run-a",
            state="queued",
            version=0,
            created_at=1000.0,
            updated_at=1000.0,
        )
    )
    assert "seal" in raw_anchor.record_calls


@pytest.mark.asyncio
async def test_factory_refuses_invalid_owner_fence(tmp_path: Path) -> None:
    raw_anchor = _Anchor()
    raw_anchor.fenced_through_epoch = "2"
    anchor = cast(arctrust.QueueBrokerAnchor, raw_anchor)
    with pytest.raises(ValueError, match="queue broker owner fence"):
        await open_durable_queue(anchor, journal_path=tmp_path / "journal.sqlite")


@pytest.mark.asyncio
async def test_factory_fences_old_running_call_without_resending(tmp_path: Path) -> None:
    anchor = cast(arctrust.QueueBrokerAnchor, _Anchor())
    cipher = arctrust.VaultRecordCipher(
        arctrust.BrokerQueueByteCipher(anchor),
        tenant_id="tenant-a",
        journal_scope="queue/tenant-a",
        purpose="queue.metadata",
    )
    path = tmp_path / "queue" / "journal.sqlite"
    journal = arcrun.create_queue_journal(
        path, cipher, anchor, recovery_authority=anchor, tenant_scope="tenant-a"
    )
    await journal.create(
        arcrun.CallJob(
            call_id="old-running",
            tenant_id="tenant-a",
            owner_id="1:run-a",
            agent_id=None,
            session_id=None,
            run_id="run-a",
            state="running",
            version=0,
            created_at=1000.0,
            updated_at=1000.0,
        )
    )
    runtime = await open_durable_queue(anchor, journal_path=path)
    assert runtime.recovered == 1
    recovered = await runtime.coordinator.store.get("old-running")
    assert recovered is not None
    assert recovered.state == "outcome_unknown"
