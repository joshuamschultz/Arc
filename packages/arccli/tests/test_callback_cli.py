"""``arc callback`` — manage the callback allowlist from the CLI (SPEC-088 T-1367, COMP-018).

Contract pinned here for the builder (T-1368), module ``arccli.commands.callback``
registered as ``arc callback``:

- ``arc callback add <origin> [--allow-private] [--clearance LEVEL]`` — bare ``<origin>`` is
  enough. Prints the new entry's id (32 lowercase hex) and its status (``active`` at
  personal; ``pending`` plus the approval id at enterprise/federal, with a
  ``work_event_callback`` approval row bound to the entry content hash that ``arc approve
  <approval id>`` activates). A refused origin (path, userinfo, query) exits 1 naming
  ``invalid_origin`` and stores nothing.
- ``arc callback list`` — one line per entry holding its id, origin and status, plus
  ``allow_private`` and the clearance name when set.
- ``arc callback remove <id>`` — the entry is ``removed`` at once; an unknown id exits 1.
- Runs under the same ``_account_authority`` context as ``arc user`` /
  ``arc principal`` (the user-store factory gains ``callbacks()`` beside ``principals()``,
  and ``AUTHORITY_RECORDS`` gains ``"callbacks"``). Without ``[security.accounts]`` it
  exits 1 with the account-authority message.

Real collaborators end to end, same harness as ``test_principal_cli``: the Vault-backed
account authority (``FakeVault`` over TLS, enrolled by the real ``arc accounts enroll``)
and the arcstore approval store over ``FakeBackend``. Every scenario starts from the bare
minimum arguments, because the untested shape is the bare call.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from arcstore.approvals import ApprovalStore, PendingApproval
from arcstore.backends.memory import FakeBackend
from arctrust.paths import config_file, users_file
from packages.arccli.tests.accounts_support import Deployment, enrolled_deployment

CALLBACK_TOOL = "work_event_callback"
ORIGIN = "https://hooks.example.com"
ID_RE = re.compile(r"\b[0-9a-f]{32}\b")


# --------------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def _isolated_arc_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARC_TEAM_ROOT", raising=False)
    monkeypatch.delenv("ARCSTORE_DATA_DIR", raising=False)
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc-home"))


@pytest.fixture
def approvals(monkeypatch: pytest.MonkeyPatch) -> FakeBackend:
    """One shared approval backend for ``arc callback`` and ``arc approve``."""
    backend = FakeBackend()
    for target in ("arcui.server", "arcui.observe", "arcstore.backends"):
        monkeypatch.setattr(f"{target}.open_backend", lambda *a, **k: backend)
    return backend


@pytest.fixture
def deployment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, approvals: FakeBackend
) -> Iterator[Deployment]:
    with enrolled_deployment(tmp_path, monkeypatch) as enrolled:
        state = users_file().parent
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        state.chmod(0o700)
        yield enrolled


@pytest.fixture
def enterprise(deployment: Deployment) -> Deployment:
    path = config_file("arcagent.toml")
    head = '[security]\ntier = "enterprise"\ncustody = "in_process"\n\n'
    path.write_text(head + path.read_text())
    return deployment


# ---------------------------------------------------------------------- helpers


def _arc(*argv: str) -> None:
    from arccli.commands.registry import resolve_command

    command = resolve_command("callback")
    assert command is not None, "`arc callback` is not a registered command"
    assert command.handler is not None
    command.handler(list(argv))


def _out(capsys: pytest.CaptureFixture[str], *argv: str) -> str:
    _arc(*argv)
    return capsys.readouterr().out


def _fails(capsys: pytest.CaptureFixture[str], *argv: str) -> str:
    with pytest.raises(SystemExit) as exited:
        _arc(*argv)
    assert exited.value.code == 1
    captured = capsys.readouterr()
    return captured.out + captured.err


def _listed(capsys: pytest.CaptureFixture[str]) -> list[str]:
    return [line for line in _out(capsys, "list").splitlines() if ID_RE.search(line)]


def _line_for(capsys: pytest.CaptureFixture[str], entry_id: str) -> str:
    (line,) = [line for line in _listed(capsys) if entry_id in line]
    return line


def _rows(backend: FakeBackend) -> list[PendingApproval]:
    async def _list() -> list[PendingApproval]:
        return await ApprovalStore(backend).list()

    return [row for row in asyncio.run(_list()) if row.tool == CALLBACK_TOOL]


def _added_id(output: str) -> str:
    ids = ID_RE.findall(output)
    assert ids, output
    return ids[0]


# ------------------------------------------------------------ personal: bare add


def test_bare_add_is_active_and_listed(
    deployment: Deployment, approvals: FakeBackend, capsys: pytest.CaptureFixture[str]
) -> None:
    output = _out(capsys, "add", ORIGIN)

    entry_id = _added_id(output)
    assert "active" in output
    line = _line_for(capsys, entry_id)
    assert ORIGIN in line
    assert "active" in line
    assert _rows(approvals) == []


def test_add_normalizes_the_origin(
    deployment: Deployment, capsys: pytest.CaptureFixture[str]
) -> None:
    entry_id = _added_id(_out(capsys, "add", "HTTPS://Hooks.Example.com:443"))
    assert ORIGIN in _line_for(capsys, entry_id)


def test_add_with_allow_private_and_clearance(
    deployment: Deployment, capsys: pytest.CaptureFixture[str]
) -> None:
    entry_id = _added_id(
        _out(capsys, "add", "https://10.0.0.5:9000", "--allow-private", "--clearance", "CUI")
    )

    line = _line_for(capsys, entry_id)
    assert "https://10.0.0.5:9000" in line
    assert "allow_private" in line
    assert "CUI" in line


@pytest.mark.parametrize(
    "origin",
    [
        "https://hooks.example.com/callbacks",
        "https://user:pw@hooks.example.com",
        "https://hooks.example.com?x=1",
    ],
)
def test_add_refuses_origin_with_path_userinfo_or_query(
    deployment: Deployment, capsys: pytest.CaptureFixture[str], origin: str
) -> None:
    output = _fails(capsys, "add", origin)

    assert "invalid_origin" in output
    assert _listed(capsys) == []


def test_list_on_an_empty_allowlist_exits_0(
    deployment: Deployment, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _listed(capsys) == []


def test_remove_marks_the_entry_removed(
    deployment: Deployment, capsys: pytest.CaptureFixture[str]
) -> None:
    entry_id = _added_id(_out(capsys, "add", ORIGIN))

    _out(capsys, "remove", entry_id)

    line = _line_for(capsys, entry_id)
    assert "removed" in line
    assert "active" not in line


def test_remove_unknown_id_exits_1(
    deployment: Deployment, capsys: pytest.CaptureFixture[str]
) -> None:
    output = _fails(capsys, "remove", "f" * 32)
    assert "unknown_entry" in output


def test_without_an_account_authority_add_exits_1(
    tmp_path: Path, approvals: FakeBackend, capsys: pytest.CaptureFixture[str]
) -> None:
    output = _fails(capsys, "add", ORIGIN)
    assert "account authority" in output.lower()


# ----------------------------------------------- enterprise: pending + arc approve


def test_enterprise_add_is_pending_with_an_approval_row(
    enterprise: Deployment, approvals: FakeBackend, capsys: pytest.CaptureFixture[str]
) -> None:
    output = _out(capsys, "add", ORIGIN)

    entry_id = _added_id(output)
    assert "pending" in output
    (row,) = _rows(approvals)
    assert row.id in output
    assert row.status == "pending"
    assert row.arguments["entry_id"] == entry_id
    assert row.arguments["origin"] == ORIGIN
    assert re.fullmatch(r"[0-9a-f]{64}", row.call_hash)
    assert "pending" in _line_for(capsys, entry_id)


def test_arc_approve_activates_the_pending_entry(
    enterprise: Deployment, approvals: FakeBackend, capsys: pytest.CaptureFixture[str]
) -> None:
    from arccli.commands.approve import approve_handler

    entry_id = _added_id(_out(capsys, "add", ORIGIN))
    (row,) = _rows(approvals)

    approve_handler([row.id])
    capsys.readouterr()

    assert "active" in _line_for(capsys, entry_id)
    (resolved,) = _rows(approvals)
    assert resolved.status == "approved"


def test_arc_approve_refuses_a_row_bound_to_another_hash(
    enterprise: Deployment, approvals: FakeBackend, capsys: pytest.CaptureFixture[str]
) -> None:
    from arccli.commands.approve import approve_handler

    entry_id = _added_id(_out(capsys, "add", ORIGIN))
    (genuine,) = _rows(approvals)
    forged = genuine.model_copy(update={"id": "cbapprove_forged0001", "call_hash": "0" * 64})
    asyncio.run(ApprovalStore(approvals).create(forged))

    with pytest.raises(SystemExit) as exited:
        approve_handler([forged.id])
    assert exited.value.code == 1
    capsys.readouterr()

    assert "pending" in _line_for(capsys, entry_id)
