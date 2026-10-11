"""`arc ui start` hands the dashboard the deployment's work-events key (SPEC-088 COMP-019).

Without it ``create_app`` starts no callback dispatcher and ``/api/work-events/keys``
answers 503 on every deployment: the producer exists and nothing ever calls it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arctrust.paths import config_file
from packages.arccli.tests.test_ui_accounts_wiring import _start_ui


@pytest.fixture(autouse=True)
def _isolated_arc_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARC_TEAM_ROOT", raising=False)
    monkeypatch.delenv("ARCSTORE_DATA_DIR", raising=False)
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc-home"))
    config_file("arcagent.toml").parent.mkdir(parents=True, exist_ok=True)
    config_file("arcagent.toml").write_text('[security]\ntier = "personal"\n')


def test_ui_start_passes_the_provisioned_work_events_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arctrust.work_event_signer import provision_work_event_key

    public_key = provision_work_event_key()

    app, kwargs = _start_ui(monkeypatch)

    signer: Any = kwargs["work_event_signer"]
    assert signer is not None
    assert signer.public_key == public_key
    assert app.state.work_event_signer is signer


def test_ui_start_without_a_work_events_key_passes_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, kwargs = _start_ui(monkeypatch)

    assert kwargs["work_event_signer"] is None
    assert app.state.work_event_signer is None


def test_ui_start_survives_a_broken_security_block(monkeypatch: pytest.MonkeyPatch) -> None:
    config_file("arcagent.toml").write_text(
        '[security]\ntier = "personal"\ncustody = "in_process"\n\n'
        '[security.vault]\naddr = "https://vault.example.test"\n'
    )

    _, kwargs = _start_ui(monkeypatch)

    assert kwargs["work_event_signer"] is None
