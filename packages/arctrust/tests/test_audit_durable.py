"""Account audit writes must fail closed on uncertain append outcomes."""

from __future__ import annotations

import os

import pytest

from arctrust.audit import AuditEvent, WormSink, verify_chain
from arctrust.keypair import generate_keypair
from arctrust.signer import InProcessSigner


def _event() -> AuditEvent:
    return AuditEvent(
        actor_did="did:arc:test:user/actor",
        action="users.change",
        target="users",
        outcome="attempt",
    )


def test_durable_append_survives_reopen(tmp_path):
    key = generate_keypair()
    path = tmp_path / "audit.worm"
    sink = WormSink(path, InProcessSigner(key.private_key))
    sink.write_durable(_event())
    sink.close()
    assert verify_chain(path, key.public_key)
    assert path.read_bytes().endswith(b"\n")


def test_partial_write_poison_refuses_future_account_audit(tmp_path, monkeypatch):
    key = generate_keypair()
    sink = WormSink(tmp_path / "audit.worm", InProcessSigner(key.private_key))
    real_write = os.write
    calls = 0

    def partial(fd, payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(fd, payload[:10])
        raise OSError("injected torn append")

    monkeypatch.setattr(os, "write", partial)
    with pytest.raises(OSError):
        sink.write_durable(_event())
    monkeypatch.setattr(os, "write", real_write)
    with pytest.raises(RuntimeError, match="uncertain"):
        sink.write_durable(_event())
    sink.close()


def test_fsync_failure_poison_refuses_success(tmp_path, monkeypatch):
    key = generate_keypair()
    sink = WormSink(tmp_path / "audit.worm", InProcessSigner(key.private_key))
    real_fsync = os.fsync

    def failed(_fd):
        raise OSError("injected disk failure")

    monkeypatch.setattr(os, "fsync", failed)
    with pytest.raises(OSError):
        sink.write_durable(_event())
    monkeypatch.setattr(os, "fsync", real_fsync)
    with pytest.raises(RuntimeError, match="uncertain"):
        sink.write_durable(_event())
    sink.close()
