"""``Connections.begin_remote_login`` / ``complete_remote_login`` against the real seam.

The shipped ``google_workspace`` manifest, a real secret store, a real registry,
and a fake ``gog`` executable at the edge (``extensions/tests/fixtures/
fake_gog.py``). Covers what the seam owns beyond the runner: the ledger across
calls, the sign-in state read afterwards, the not-installed and expired states,
and that a refusal stopped BEFORE the binary ran is still in the audit chain.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import pytest
from arcstore.backends.memory import FakeBackend
from arctrust.audit import AuditEvent

from arcagent.connections import AuditChain, Connections
from arcagent.core.errors import ExtensionError
from arcagent.extension.remote_login import (
    REMOTE_LOGIN_BUSY,
    REMOTE_LOGIN_NOT_STARTED,
    RemoteLoginLedger,
)

_REPO = Path(__file__).resolve().parents[4]
_BUNDLE = _REPO / "extensions" / "google_workspace"
_FAKE_GOG = _REPO / "extensions" / "tests" / "fixtures" / "fake_gog.py"
_ACCOUNT = "josh@blackarcindustrial.com"


class _Sink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def gog_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gog = bin_dir / "gog"
    gog.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{_FAKE_GOG}" "$@"\n', encoding="utf-8")
    gog.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_GOG_HOME", str(tmp_path / "gog"))
    monkeypatch.delenv("GOG_ACCOUNT", raising=False)
    monkeypatch.delenv("GOG_CLIENT", raising=False)
    return tmp_path / "gog"


async def _open(backend: FakeBackend) -> FakeBackend:
    return backend


async def _connected(
    tmp_path: Path, sink: _Sink, ledger: RemoteLoginLedger, *accounts: tuple[str, str]
) -> Connections:
    root = tmp_path / "extensions"
    shutil.copytree(
        _BUNDLE, root / "google_workspace", ignore=shutil.ignore_patterns("__pycache__")
    )
    backend = FakeBackend()
    connections = Connections.for_deployment(
        arc_dir=tmp_path / "arc",
        data_dir=tmp_path / "data",
        extensions_root=root,
        audit=AuditChain.held(sink),
        state_opener=lambda: _open(backend),
        remote_logins=ledger,
    )
    for instance, account in accounts:
        plan = connections.plan("google_workspace", instance)
        await connections.install(plan, {"account": account})
    return connections


def _landed(consent_url: str, who: str) -> str:
    query = parse_qs(urlsplit(consent_url).query)
    code = quote(f"code-for:{who}", safe="")
    return f"{query['redirect_uri'][0]}?state={query['state'][0]}&code={code}&scope=email"


async def test_a_sign_in_begins_completes_and_reads_working(
    tmp_path: Path, gog_home: Path
) -> None:
    sink = _Sink()
    connections = await _connected(tmp_path, sink, RemoteLoginLedger(), ("blackarc", _ACCOUNT))
    assert (await connections.authorization("blackarc")).sign_in == "signed_out"

    started = await connections.begin_remote_login("blackarc")
    assert started.account == _ACCOUNT
    auth = await connections.complete_remote_login(
        "blackarc", redirect_url=_landed(started.consent_url, _ACCOUNT)
    )

    assert auth.sign_in == "signed_in"
    assert auth.working
    assert auth.remote_login
    assert auth.manual_command == "", "a browser-finishable sign-in sends nobody to a terminal"
    actions = [event.action for event in sink.events if "remote_login" in event.action]
    assert actions == [
        "extension.host.remote_login.begin",
        "extension.host.remote_login.complete",
    ]


async def test_an_expired_token_reads_expired(tmp_path: Path, gog_home: Path) -> None:
    connections = await _connected(tmp_path, _Sink(), RemoteLoginLedger(), ("blackarc", _ACCOUNT))
    gog_home.mkdir(exist_ok=True)
    (gog_home / "tokens.json").write_text(json.dumps({f"default:{_ACCOUNT}": "expired"}))

    doctor = {row.check: row for row in await connections.doctor("blackarc")}

    assert doctor["sign-in"].status == "expired"
    assert "invalid_grant" in doctor["sign-in"].detail


async def test_a_missing_binary_reads_not_installed(
    tmp_path: Path, gog_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connections = await _connected(tmp_path, _Sink(), RemoteLoginLedger(), ("blackarc", _ACCOUNT))
    monkeypatch.setenv("PATH", "/nonexistent")

    auth = await connections.authorization("blackarc")

    assert auth.sign_in == "not_installed"


async def test_a_busy_ledger_refusal_is_audited_before_anything_runs(
    tmp_path: Path, gog_home: Path
) -> None:
    sink = _Sink()
    connections = await _connected(
        tmp_path,
        sink,
        RemoteLoginLedger(),
        ("blackarc", _ACCOUNT),
        ("systems", "josh@blackarcsystems.com"),
    )
    await connections.begin_remote_login("blackarc")

    with pytest.raises(ExtensionError) as refused:
        await connections.begin_remote_login("systems")

    assert refused.value.code == REMOTE_LOGIN_BUSY
    denied = [event for event in sink.events if event.outcome == "deny"]
    assert denied[-1].action == "extension.host.remote_login.begin"
    assert denied[-1].extra["reason"] == REMOTE_LOGIN_BUSY
    assert denied[-1].extra["connection"] == "systems"


async def test_a_begun_sign_in_expires(tmp_path: Path, gog_home: Path) -> None:
    clock = _Clock()
    ledger = RemoteLoginLedger(ttl=60, clock=clock)
    connections = await _connected(tmp_path, _Sink(), ledger, ("blackarc", _ACCOUNT))
    started = await connections.begin_remote_login("blackarc")
    clock.now += 61

    with pytest.raises(ExtensionError) as refused:
        await connections.complete_remote_login(
            "blackarc", redirect_url=_landed(started.consent_url, _ACCOUNT)
        )

    assert refused.value.code == REMOTE_LOGIN_NOT_STARTED


async def test_a_spent_code_asks_for_a_fresh_start(tmp_path: Path, gog_home: Path) -> None:
    """Once the binary ran, the begun sign-in is gone — its code was single-use."""
    connections = await _connected(tmp_path, _Sink(), RemoteLoginLedger(), ("blackarc", _ACCOUNT))
    started = await connections.begin_remote_login("blackarc")
    landed = _landed(started.consent_url, "someone@else.example")
    with pytest.raises(ExtensionError):
        await connections.complete_remote_login("blackarc", redirect_url=landed)

    with pytest.raises(ExtensionError) as again:
        await connections.complete_remote_login("blackarc", redirect_url=landed)

    assert again.value.code == REMOTE_LOGIN_NOT_STARTED
