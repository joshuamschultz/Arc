"""Queue injection remains on the public ArcRun model facade."""

from pathlib import Path
from unittest.mock import Mock

import arcllm
from arctrust import AnchorHead, RecordCipher

import arcrun


class _Anchor:
    def __init__(self) -> None:
        self.head: AnchorHead | None = None

    @property
    def scope(self) -> str:
        return "queue/test"

    def latest(self) -> AnchorHead | None:
        return self.head

    def compare_and_advance(
        self, expected: AnchorHead | None, digest: str, intent: str
    ) -> AnchorHead:
        assert expected == self.head
        self.head = AnchorHead(
            scope=self.scope,
            version=1 if expected is None else expected.version + 1,
            digest=digest,
            previous_digest=expected.digest if expected else None,
            intent=intent,
        )
        return self.head


def test_model_facade_passes_explicit_queue_owner(monkeypatch) -> None:
    returned = object()
    loader = Mock(return_value=returned)
    monkeypatch.setattr(arcllm, "load_model", loader)
    coordinator = arcrun.CallQueueCoordinator()
    context = arcrun.CallQueueContext("tenant", "run", "call")
    assert (
        arcrun.load_model(
            "anthropic",
            queue_coordinator=coordinator,
            queue_context=context,
        )
        is returned
    )
    assert loader.call_args.kwargs["queue_coordinator"] is coordinator
    assert loader.call_args.kwargs["queue_context"] is context


def test_model_facade_creates_anchored_encrypted_store(tmp_path: Path) -> None:
    store = arcrun.create_queue_journal(
        tmp_path / "calls.sqlite", RecordCipher(b"k" * 32), _Anchor()
    )
    assert isinstance(store, arcllm.QueueJournal)


def test_queue_context_facade_restores_previous_task_scope() -> None:
    coordinator = arcrun.CallQueueCoordinator()
    first = arcrun.CallQueueContext("tenant", "owner", run_id="first")
    second = arcrun.CallQueueContext("tenant", "owner", run_id="second")
    with arcrun.queue_run_context(coordinator, first):
        assert coordinator.current_context is first
        with arcrun.queue_run_context(coordinator, second):
            assert coordinator.current_context is second
        assert coordinator.current_context is first
    assert coordinator.current_context is None
