"""``arc stop`` — the on-box operator surface writes an attributable cancel request.

Proves the write half of the kill switch: ``arc stop <run_id>`` parks a ``pending``
row in the shared ``cancellations`` directory, attributed to the deployment operator
DID, that the target agent's watcher later applies.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from arcstore.backends.memory import FakeBackend
from arcstore.cancellations import CancelRequest, CancelStore

from arccli.commands.stop import stop_handler


@pytest.fixture(autouse=True)
def _isolated_arc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Isolate both the operator key (ARC_CONFIG_DIR) and the store db
    # (ARCSTORE_DATA_DIR) so the test never touches the real ~/.arc.
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(tmp_path))


@pytest.fixture
def fake_backend(monkeypatch: pytest.MonkeyPatch) -> FakeBackend:
    """Inject the backend seam; production ``open_backend`` is PostgreSQL-only."""
    backend = FakeBackend()

    def _open_backend(**_kwargs: object) -> FakeBackend:
        return backend

    monkeypatch.setattr("arcstore.backends.open_backend", _open_backend)
    return backend


async def _list_pending(backend: FakeBackend) -> list[CancelRequest]:
    await backend.start()
    try:
        return await CancelStore(backend).list(status="pending")
    finally:
        await backend.stop()


def _operator_did() -> str:
    from arctrust.policy import OperatorApprovalAuthority

    from arccli.commands.operator import resolve_operator_signer

    return OperatorApprovalAuthority(resolve_operator_signer()).did


def test_stop_writes_pending_request_attributed_to_operator(fake_backend: FakeBackend) -> None:
    stop_handler(["run-abc", "--reason", "too long"])

    pending = asyncio.run(_list_pending(fake_backend))
    assert len(pending) == 1
    req = pending[0]
    assert req.run_id == "run-abc"
    assert req.reason == "too long"
    assert req.status == "pending"
    assert req.requested_by == _operator_did()


def test_stop_by_session_key(fake_backend: FakeBackend) -> None:
    stop_handler(["--session", "cli:main"])

    pending = asyncio.run(_list_pending(fake_backend))
    assert len(pending) == 1
    assert pending[0].session_key == "cli:main"
    assert pending[0].run_id == ""


def test_stop_list_shows_pending(
    capsys: pytest.CaptureFixture[str], fake_backend: FakeBackend
) -> None:
    stop_handler(["run-abc"])
    capsys.readouterr()  # drop the create confirmation

    stop_handler(["list"])

    out = capsys.readouterr().out
    assert "run-abc" in out


def test_stop_with_no_target_prints_help(
    capsys: pytest.CaptureFixture[str], fake_backend: FakeBackend
) -> None:
    stop_handler([])

    out = capsys.readouterr().out
    assert "usage" in out.lower()
    assert asyncio.run(_list_pending(fake_backend)) == []
