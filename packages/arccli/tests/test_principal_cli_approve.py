"""``arc approve`` resolves a ``principal_issue`` request (SPEC-087 T-1287, REQ-552/553/554).

An enterprise or federal ``arc principal issue`` leaves the principal pending and
an approval row bound to its content hash. Approving the row from the CLI must
activate exactly that record; a row whose hash no longer matches the record is
refused; denying it revokes the record. At federal the CLI has no person
identity to compare against the requester, so it refuses and points to the
dashboard (two-person rule, REQ-554).

Same real collaborators as ``test_principal_cli``: the Vault-backed authority
over ``FakeVault``, the arcstore approval store over ``FakeBackend`` and the
real arcui ``AuthMiddleware`` for the end-to-end bearer check.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from arcstore.approvals import ApprovalStore, PendingApproval
from arcstore.backends.memory import FakeBackend
from arctrust.paths import config_file, users_file
from packages.arccli.tests.accounts_support import Deployment, enrolled_deployment
from packages.arccli.tests.test_principal_cli import (
    _agent_did,
    _approval_rows,
    _bare_issue,
    _get_run,
    _issued_bearer,
    _only_principal,
    _operator_account,
    _principal_app,
)

from arccli.commands.approve import approve_handler


@pytest.fixture(autouse=True)
def _isolated_arc_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARC_TEAM_ROOT", raising=False)
    monkeypatch.delenv("ARCSTORE_DATA_DIR", raising=False)
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc-home"))


@pytest.fixture
def approvals(monkeypatch: pytest.MonkeyPatch) -> FakeBackend:
    backend = FakeBackend()
    # arcui binds ``open_backend`` at import; patch its names too (as arcui's own
    # conftest does) so the in-process app never reaches for PostgreSQL, whatever
    # imported arcui first in this session.
    for target in ("arcui.server", "arcui.observe", "arcstore.backends"):
        monkeypatch.setattr(f"{target}.open_backend", lambda *a, **k: backend)
    return backend


@pytest.fixture
def enterprise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, approvals: FakeBackend
) -> Iterator[Deployment]:
    with enrolled_deployment(tmp_path, monkeypatch) as enrolled:
        state = users_file().parent
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        state.chmod(0o700)
        _set_security('tier = "enterprise"\ncustody = "in_process"')
        yield enrolled


def _set_security(body: str) -> None:
    """Replace (or add) the leading ``[security]`` block beside ``[security.accounts]``."""
    path = config_file("arcagent.toml")
    text = path.read_text()
    accounts = text[text.index("[security.accounts]") :]
    path.write_text(f"[security]\n{body}\n\n{accounts}")


def _row(backend: FakeBackend) -> PendingApproval:
    [row] = _approval_rows(backend)
    return row


def _approve_fails(capsys: pytest.CaptureFixture[str], *argv: str) -> str:
    with pytest.raises(SystemExit) as exited:
        approve_handler(list(argv))
    assert exited.value.code == 1
    captured = capsys.readouterr()
    return captured.out + captured.err


def test_approving_the_row_activates_the_principal_and_its_bearer_works(
    enterprise: Deployment,
    approvals: FakeBackend,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _operator_account(enterprise)
    _bare_issue(tmp_path, _agent_did(), "--expires", "30d")
    bearer = _issued_bearer(capsys.readouterr().out)
    row = _row(approvals)
    rig = _principal_app(enterprise, tmp_path, "enterprise")
    assert _get_run(rig, bearer) == 401  # pending: unusable

    approve_handler([row.id])

    record = _only_principal(enterprise)
    assert record.status == "active"
    assert record.approved_by is not None
    assert _row(approvals).status == "approved"
    assert record.id in capsys.readouterr().out
    assert _get_run(rig, bearer) == 200


def test_a_row_whose_hash_does_not_match_the_record_is_refused(
    enterprise: Deployment,
    approvals: FakeBackend,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A row naming the principal but bound to other content activates nothing."""
    _operator_account(enterprise)
    _bare_issue(tmp_path, _agent_did(), "--expires", "30d")
    capsys.readouterr()
    genuine = _row(approvals)
    forged = genuine.model_copy(update={"id": "prinissue_forged0001", "call_hash": "0" * 64})
    asyncio.run(ApprovalStore(approvals).create(forged))

    output = _approve_fails(capsys, forged.id)

    assert "content_hash_mismatch" in output
    assert _only_principal(enterprise).status == "pending"
    rows = {row.id: row.status for row in _approval_rows(approvals)}
    assert rows == {genuine.id: "pending", forged.id: "pending"}


def test_denying_the_row_revokes_the_principal(
    enterprise: Deployment,
    approvals: FakeBackend,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _operator_account(enterprise)
    _bare_issue(tmp_path, _agent_did(), "--expires", "30d")
    capsys.readouterr()
    row = _row(approvals)

    approve_handler([row.id, "--deny"])

    assert _row(approvals).status == "denied"
    record = _only_principal(enterprise)
    assert record.status == "revoked"
    assert record.revoked_by is not None


def test_always_cannot_make_a_principal_request_stand(
    enterprise: Deployment,
    approvals: FakeBackend,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--always`` is for tool calls; a principal approval is one record, once."""
    _operator_account(enterprise)
    _bare_issue(tmp_path, _agent_did(), "--expires", "30d")
    capsys.readouterr()
    row = _row(approvals)

    output = _approve_fails(capsys, row.id, "--always")

    assert "--always" in output
    assert _only_principal(enterprise).status == "pending"
    assert _row(approvals).status == "pending"


def test_federal_refuses_cli_approval_and_points_to_the_dashboard(
    enterprise: Deployment,
    approvals: FakeBackend,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """REQ-554: the CLI acts as the deployment key, not a person, so it cannot prove
    the approver is someone other than the requester."""
    _operator_account(enterprise)
    _bare_issue(tmp_path, _agent_did(), "--expires", "30d")
    capsys.readouterr()
    row = _row(approvals)
    _set_security('tier = "federal"')

    output = _approve_fails(capsys, row.id)

    assert "dashboard" in output.lower()
    assert "federal" in output.lower()
    assert _row(approvals).status == "pending"
    _set_security('tier = "enterprise"\ncustody = "in_process"')
    assert _only_principal(enterprise).status == "pending"
