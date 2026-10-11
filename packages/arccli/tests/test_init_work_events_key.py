"""SPEC-088 T-1369 (RED): ``arc init`` provisions the work-events signing key (COMP-019).

Contract pinned for the builder (T-1370): ``arccli.commands.init._init`` calls
``arctrust.work_event_signer.provision_work_event_key`` right after
``ensure_operator_key``, with the security view of the deployment it just wrote. After a
bare personal ``arc init`` the deployment has a work-events signer
(``work_event_signer_for(machine_security(base), base=base)`` is not ``None``) whose public
key is NOT the operator key. Re-running ``arc init`` keeps the same key (idempotent, never
overwritten), exactly as it keeps the operator key.

Driven the way the existing operator-key init tests drive it: ``_init`` with an
``argparse.Namespace`` and the bare minimum arguments (``arc init --quick``).
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest
from arctrust import machine_security, verify_signature
from arctrust.operator import OperatorKey
from arctrust.paths import default_operator_key_path

from arccli.commands.init import _init


@pytest.fixture(autouse=True)
def _arc_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every resolver in this test answers for the deployment ``arc init`` writes."""
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))


def _run_init(config_dir: Path) -> None:
    """``arc init --quick --dir <config_dir>``: personal, zero prompts, re-runnable."""
    _init(argparse.Namespace(tier=None, config_dir=str(config_dir), provider=None, quick=True))


def _signer(config_dir: Path) -> Any:
    from arctrust.work_event_signer import work_event_signer_for

    return work_event_signer_for(machine_security(config_dir), base=config_dir)


def test_init_provisions_a_work_events_key_that_is_not_the_operator_key(tmp_path: Path) -> None:
    _run_init(tmp_path)

    signer = _signer(tmp_path)
    operator = OperatorKey.load(default_operator_key_path(tmp_path))

    assert signer is not None, "arc init left the deployment without a work-events key"
    assert signer.public_key != operator.public_key
    signature = signer.sign(b"arc.work-event.v1")
    assert verify_signature(signer.algorithm, b"arc.work-event.v1", signature, signer.public_key)
    assert not verify_signature(
        signer.algorithm, b"arc.work-event.v1", signature, operator.public_key
    )


def test_rerunning_init_keeps_the_same_work_events_key(tmp_path: Path) -> None:
    _run_init(tmp_path)
    first = _signer(tmp_path)
    assert first is not None

    _run_init(tmp_path)
    again = _signer(tmp_path)

    assert again is not None
    assert again.public_key == first.public_key, "re-running arc init replaced the key"
    assert again.key_id == first.key_id
