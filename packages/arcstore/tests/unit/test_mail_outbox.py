from pathlib import Path

import pytest

from arcstore.mail_outbox import MailOutbox


def test_outbox_claim_ack_is_idempotent_and_restart_safe(tmp_path: Path) -> None:
    path = tmp_path / "mail-outbox.jsonl"
    first = MailOutbox(path, lease_seconds=0.01)
    first.enqueue("mail-1", {"id": "mail-1", "body": "hello"})
    assert [item.event_id for item in first.claim("worker-a")] == ["mail-1"]
    assert first.claim("worker-b") == ()
    assert first.ack("worker-a", "mail-1") is True
    assert first.ack("worker-a", "mail-1") is False

    restarted = MailOutbox(path)
    assert restarted.pending() == ()
    restarted.enqueue("mail-1", {"id": "mail-1", "body": "changed"})
    assert [item.event_id for item in restarted.pending()] == ["mail-1"]


def test_outbox_nack_backoff_and_expired_lease_can_restart(tmp_path: Path) -> None:
    path = tmp_path / "mail-outbox.jsonl"
    outbox = MailOutbox(path, lease_seconds=0.01)
    outbox.enqueue("mail-1", {"id": "mail-1"})
    assert outbox.claim("worker-a")
    assert outbox.nack("worker-a", "mail-1", retry_after_seconds=60) is True
    assert outbox.claim("worker-b") == ()


def test_outbox_rejects_invalid_claim_inputs(tmp_path: Path) -> None:
    outbox = MailOutbox(tmp_path / "mail-outbox.jsonl")
    with pytest.raises(ValueError):
        outbox.claim("", limit=1)
    with pytest.raises(ValueError):
        outbox.claim("worker", limit=0)
