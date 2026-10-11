"""``arc callback key`` mints the work-events key on a deployment that predates it.

SPEC-088 COMP-019 / DEV-15: ``arc init`` mints the key, but the deploy scripts skip
``arc init`` on a box that already has config, so every deployment initialised
before SPEC-088 has no work-events key and never sends a callback. This verb mints
it in place (idempotent, never the operator key) and fails loud when custody cannot
hold it, so the operator learns why instead of finding ``notifications_unavailable``.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest
from arctrust import machine_security
from arctrust.operator import OperatorKey
from arctrust.paths import default_operator_key_path
from arctrust.signer import SignerError

from arccli.commands import init as init_cmd
from arccli.commands.callback import callback_handler


@pytest.fixture(autouse=True)
def _arc_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))


@pytest.fixture
def pre_088_deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A personal deployment written by an ``arc init`` that minted no work-events key."""
    with monkeypatch.context() as patch:
        patch.setattr(init_cmd, "_provision_work_events_key", lambda _arc_dir: None)
        init_cmd._init(
            argparse.Namespace(tier=None, config_dir=str(tmp_path), provider=None, quick=True)
        )
    assert _signer(tmp_path) is None
    return tmp_path


def _signer(config_dir: Path) -> Any:
    from arctrust.work_event_signer import work_event_signer_for

    return work_event_signer_for(machine_security(config_dir), base=config_dir)


def test_key_mints_the_work_events_key_on_an_existing_deployment(
    pre_088_deployment: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    callback_handler(["key"])

    signer = _signer(pre_088_deployment)
    assert signer is not None, "arc callback key left the deployment without a key"
    operator = OperatorKey.load(default_operator_key_path(pre_088_deployment))
    assert signer.public_key != operator.public_key
    assert signer.key_id in capsys.readouterr().out


def test_key_is_idempotent(pre_088_deployment: Path, capsys: pytest.CaptureFixture[str]) -> None:
    callback_handler(["key"])
    first = _signer(pre_088_deployment)
    callback_handler(["key"])
    again = _signer(pre_088_deployment)

    assert first is not None and again is not None
    assert again.public_key == first.public_key, "re-running replaced the key"


def test_key_fails_loud_when_custody_cannot_hold_it(
    pre_088_deployment: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def refuse(*_a: Any, **_k: Any) -> bytes:
        raise SignerError("vault unreachable")

    monkeypatch.setattr("arctrust.work_event_signer.provision_work_event_key", refuse)

    with pytest.raises(SystemExit) as exc:
        callback_handler(["key"])

    assert exc.value.code == 1
    assert "vault unreachable" in capsys.readouterr().err
    assert _signer(pre_088_deployment) is None
