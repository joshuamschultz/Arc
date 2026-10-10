"""``arc principal`` — issue, list, show and revoke app principals (SPEC-087 T-1287).

Real collaborators end to end: the command runs through the CLI registry against
the Vault-backed account authority (``FakeVault`` over TLS, enrolled by the real
``arc accounts enroll``), the arcstore approval store (``FakeBackend``, the
contract fake every approval test uses) and, for revocation, the real arcui
``AuthMiddleware`` reading the SAME principal record. Nothing of the command or
the store is mocked.

Every scenario starts from the bare minimum arguments, because the untested
shape is the bare call: ``issue --name N --public-key-file F --scope
runs:read=<agent did>``.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from arcstore.approvals import ApprovalStore, PendingApproval
from arcstore.backends.memory import FakeBackend
from arctrust.canonical import canonical_json
from arctrust.identity import did_from_public_key, did_matches_pubkey, parse_did
from arctrust.keypair import generate_keypair
from arctrust.paths import config_file, users_file
from arctrust.principal_auth import parse_bearer, secret_digest
from packages.arccli.tests.accounts_support import (
    Deployment,
    RecordingSink,
    enrolled_deployment,
)

from arccli.commands._accounts import build_user_store_factory

PASSWORD = "correct-horse-battery"
BEARER_RE = re.compile(r"arcp_[0-9a-f]{32}\.[A-Za-z0-9_-]{43}")
PRINCIPAL_ISSUE_TOOL = "principal_issue"


# --------------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def _isolated_arc_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Own Arc home per test, even in the cross-package battery (no arccli conftest)."""
    monkeypatch.delenv("ARC_TEAM_ROOT", raising=False)
    monkeypatch.delenv("ARCSTORE_DATA_DIR", raising=False)
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc-home"))


@pytest.fixture
def approvals(monkeypatch: pytest.MonkeyPatch) -> FakeBackend:
    """One shared approval backend for ``arc principal`` and ``arc approve``.

    Patched at the canonical constructor (``arcstore.backends.open_backend``), so
    whatever the command opens per call is this fake; nothing else is replaced.
    """
    backend = FakeBackend()
    # arcui binds ``open_backend`` at import; patch its names too (as arcui's own
    # conftest does) so the in-process app never reaches for PostgreSQL, whatever
    # imported arcui first in this session.
    for target in ("arcui.server", "arcui.observe", "arcstore.backends"):
        monkeypatch.setattr(f"{target}.open_backend", lambda *a, **k: backend)
    return backend


@pytest.fixture
def deployment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, approvals: FakeBackend
) -> Iterator[Deployment]:
    with enrolled_deployment(tmp_path, monkeypatch) as enrolled:
        # The default record directory (``<arc_state>``) is private on a real
        # deployment; the sealed store refuses anything else. Both the CLI and
        # arcui use the default path here, so they read one record.
        state = users_file().parent
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        state.chmod(0o700)
        yield enrolled


@pytest.fixture
def enterprise(deployment: Deployment) -> Deployment:
    _set_tier("enterprise")
    return deployment


def _set_tier(tier: str) -> None:
    """Put the deployment at ``tier`` beside its ``[security.accounts]`` block.

    Custody stays in-process so the CLI's operator audit chain needs no Vault
    Transit key (enterprise would otherwise default custody to vault_transit);
    the principal rules under test depend on the tier alone.
    """
    path = config_file("arcagent.toml")
    head = f'[security]\ntier = "{tier}"\ncustody = "in_process"\n\n'
    path.write_text(head + path.read_text())


# ---------------------------------------------------------------------- helpers


def _authority(deployment: Deployment) -> Any:
    """The same Vault-backed authority arcui serves from (users + principals)."""
    factory = build_user_store_factory(RecordingSink(), users_path=None)
    assert factory is not None
    return factory


def _operator_account(deployment: Deployment) -> Any:
    return _authority(deployment)().add("ada@example.com", PASSWORD, roles=("operator",))


def _arc(*argv: str) -> None:
    """Run ``arc principal <argv>`` the way the CLI dispatches it."""
    from arccli.commands.registry import resolve_command

    command = resolve_command("principal")
    assert command is not None, "`arc principal` is not a registered command"
    assert command.handler is not None
    command.handler(list(argv))


def _arc_fails(capsys: pytest.CaptureFixture[str], *argv: str) -> str:
    """Run a command that must exit 1; return everything it wrote."""
    with pytest.raises(SystemExit) as exited:
        _arc(*argv)
    assert exited.value.code == 1
    captured = capsys.readouterr()
    return captured.out + captured.err


def _public_key_file(tmp_path: Path) -> tuple[Path, bytes]:
    """An app's own Ed25519 verify key, hex-encoded (the format ``arc team register`` takes)."""
    public_key = generate_keypair().public_key
    path = tmp_path / "app.pub"
    path.write_text(public_key.hex() + "\n")
    return path, public_key


def _agent_did() -> str:
    return did_from_public_key(generate_keypair().public_key, org="acme", agent_type="executor")


def _bare_issue(tmp_path: Path, agent: str, *extra: str, name: str = "billing-bot") -> bytes:
    key_file, public_key = _public_key_file(tmp_path)
    _arc(
        "issue",
        "--name",
        name,
        "--public-key-file",
        str(key_file),
        "--scope",
        f"runs:read={agent}",
        *extra,
    )
    return public_key


def _only_principal(deployment: Deployment) -> Any:
    principals = _authority(deployment).principals().list()
    assert len(principals) == 1
    return principals[0]


def _approval_rows(backend: FakeBackend) -> list[PendingApproval]:
    async def _list() -> list[PendingApproval]:
        return await ApprovalStore(backend).list()

    return [row for row in asyncio.run(_list()) if row.tool == PRINCIPAL_ISSUE_TOOL]


def _issued_bearer(output: str) -> str:
    tokens = BEARER_RE.findall(output)
    assert len(tokens) == 1, f"expected the bearer exactly once, got {len(tokens)}"
    return tokens[0]


def _assert_no_secret(output: str, record: Any) -> None:
    assert BEARER_RE.search(output) is None
    assert record.secret_digest not in output


# ------------------------------------------------------- personal: bare issue


def test_bare_personal_issue_prints_the_did_and_the_bearer_once_and_is_active(
    deployment: Deployment, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _operator_account(deployment)
    agent = _agent_did()

    public_key = _bare_issue(tmp_path, agent)

    captured = capsys.readouterr()
    record = _only_principal(deployment)
    assert record.status == "active"
    assert record.name == "billing-bot"
    assert {str(s): list(a) for s, a in record.scopes.items()} == {"runs:read": [agent]}
    # The DID is the app's own: derived from the key it supplied, agent_type app.
    assert did_matches_pubkey(record.did, public_key)
    assert parse_did(record.did)["agent_type"] == "app"
    assert record.did in captured.out
    # The secret goes to stdout exactly once and nowhere else; only its digest is kept.
    bearer = _issued_bearer(captured.out)
    assert BEARER_RE.search(captured.err) is None
    principal_id, secret = parse_bearer(bearer)
    assert principal_id == record.id
    assert record.secret_digest == secret_digest(secret)
    assert secret not in json.dumps(record.to_dict())


def test_personal_issue_without_scope_is_refused(
    deployment: Deployment, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every app scope is agent-bound; a principal that can do nothing is a mistake."""
    _operator_account(deployment)
    key_file, _ = _public_key_file(tmp_path)

    with pytest.raises(SystemExit) as exited:
        _arc("issue", "--name", "nothing-bot", "--public-key-file", str(key_file))

    assert exited.value.code != 0
    assert _authority(deployment).principals().list() == []


# --------------------------------------------------------------- refusals


def test_unknown_scope_exits_1_with_a_clear_message_and_stores_nothing(
    deployment: Deployment, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _operator_account(deployment)
    key_file, _ = _public_key_file(tmp_path)

    output = _arc_fails(
        capsys,
        "issue",
        "--name",
        "billing-bot",
        "--public-key-file",
        str(key_file),
        "--scope",
        f"admin:everything={_agent_did()}",
    )

    assert "admin:everything" in output
    assert "scope" in output.lower()
    assert BEARER_RE.search(output) is None
    assert _authority(deployment).principals().list() == []


def test_scope_without_an_agent_binding_exits_1(
    deployment: Deployment, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _operator_account(deployment)
    key_file, _ = _public_key_file(tmp_path)

    output = _arc_fails(
        capsys,
        "issue",
        "--name",
        "billing-bot",
        "--public-key-file",
        str(key_file),
        "--scope",
        "runs:read",
    )

    assert "runs:read" in output
    assert _authority(deployment).principals().list() == []


def test_a_public_key_file_that_is_not_an_ed25519_key_exits_1(
    deployment: Deployment, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _operator_account(deployment)
    bad = tmp_path / "bad.pub"
    bad.write_text("not-a-key\n")

    output = _arc_fails(
        capsys,
        "issue",
        "--name",
        "billing-bot",
        "--public-key-file",
        str(bad),
        "--scope",
        f"runs:read={_agent_did()}",
    )

    assert "key" in output.lower()
    assert _authority(deployment).principals().list() == []


def test_without_an_account_authority_every_subcommand_fails_clearly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], approvals: FakeBackend
) -> None:
    """No [security.accounts]: a clear error and exit 1, never a traceback."""
    key_file, _ = _public_key_file(tmp_path)
    attempts: list[tuple[str, ...]] = [
        ("issue", "--name", "b", "--public-key-file", str(key_file), "--scope", "runs:read=x"),
        ("list",),
        ("show", "0" * 32),
        ("revoke", "0" * 32),
    ]
    for argv in attempts:
        output = _arc_fails(capsys, *argv)
        assert "account authority is unavailable" in output, argv
        assert "[security.accounts]" in output, argv


# ------------------------------------------------------------- subjects


def test_subject_emails_resolve_to_user_dids(
    deployment: Deployment, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ada = _operator_account(deployment)
    bob = _authority(deployment)().add("bob@example.com", PASSWORD, roles=("viewer",))

    _bare_issue(tmp_path, _agent_did(), "--subject", "ada@example.com", "--subject", bob.did)

    record = _only_principal(deployment)
    assert sorted(record.subjects) == sorted([ada.did, bob.did])


def test_an_unknown_subject_email_exits_1_and_stores_nothing(
    deployment: Deployment, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _operator_account(deployment)
    key_file, _ = _public_key_file(tmp_path)

    output = _arc_fails(
        capsys,
        "issue",
        "--name",
        "billing-bot",
        "--public-key-file",
        str(key_file),
        "--scope",
        f"runs:read={_agent_did()}",
        "--subject",
        "nobody@example.com",
    )

    assert "nobody@example.com" in output
    assert BEARER_RE.search(output) is None
    assert _authority(deployment).principals().list() == []


# ------------------------------------------------------------ enterprise


def test_bare_enterprise_issue_is_pending_with_an_approval_row_bound_to_the_record(
    enterprise: Deployment,
    approvals: FakeBackend,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _operator_account(enterprise)
    agent = _agent_did()

    _bare_issue(tmp_path, agent, "--expires", "30d")

    out = capsys.readouterr().out
    record = _only_principal(enterprise)
    assert record.status == "pending"
    assert record.expires_at is not None
    [row] = _approval_rows(approvals)
    assert row.status == "pending"
    assert row.call_hash == record.content_hash()
    assert row.arguments == {
        "principal_id": record.id,
        "name": "billing-bot",
        "scopes": canonical_json({"runs:read": [agent]}).decode("utf-8"),
    }
    assert "pending" in out.lower()
    assert row.id in out
    assert f"arc approve {row.id}" in out
    # The secret is only ever shown at request time, so it is shown now: once.
    _, secret = parse_bearer(_issued_bearer(out))
    assert record.secret_digest == secret_digest(secret)


def test_federal_issue_is_refused_and_writes_nothing(
    deployment: Deployment,
    approvals: FakeBackend,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Federal (REQ-554): the deployment key is not a person, so a CLI request
    could never be checked against the second approver. Issue in the dashboard."""
    # Federal requires Transit custody; the refusal comes before any custody use.
    path = config_file("arcagent.toml")
    path.write_text(
        '[security]\ntier = "federal"\ncustody = "vault_transit"\n\n' + path.read_text()
    )
    key_file, _ = _public_key_file(tmp_path)

    output = _arc_fails(
        capsys,
        "issue",
        "--name",
        "billing-bot",
        "--public-key-file",
        str(key_file),
        "--scope",
        f"runs:read={_agent_did()}",
        "--expires",
        "7d",
    )

    assert "dashboard" in output.lower()
    assert BEARER_RE.search(output) is None
    assert _approval_rows(approvals) == []


def test_enterprise_issue_without_expiry_exits_1_and_writes_nothing(
    enterprise: Deployment,
    approvals: FakeBackend,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _operator_account(enterprise)
    key_file, _ = _public_key_file(tmp_path)

    output = _arc_fails(
        capsys,
        "issue",
        "--name",
        "billing-bot",
        "--public-key-file",
        str(key_file),
        "--scope",
        f"runs:read={_agent_did()}",
    )

    assert "expir" in output.lower()
    assert BEARER_RE.search(output) is None
    assert _authority(enterprise).principals().list() == []
    assert _approval_rows(approvals) == []


def test_enterprise_issue_beyond_90_days_exits_1(
    enterprise: Deployment,
    approvals: FakeBackend,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _operator_account(enterprise)
    key_file, _ = _public_key_file(tmp_path)

    output = _arc_fails(
        capsys,
        "issue",
        "--name",
        "billing-bot",
        "--public-key-file",
        str(key_file),
        "--scope",
        f"runs:read={_agent_did()}",
        "--expires",
        "91d",
    )

    assert "expir" in output.lower()
    assert _authority(enterprise).principals().list() == []
    assert _approval_rows(approvals) == []


def test_enterprise_refuses_a_wildcard_agent_binding(
    enterprise: Deployment,
    approvals: FakeBackend,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _operator_account(enterprise)
    key_file, _ = _public_key_file(tmp_path)

    output = _arc_fails(
        capsys,
        "issue",
        "--name",
        "billing-bot",
        "--public-key-file",
        str(key_file),
        "--scope",
        "runs:read=*",
        "--expires",
        "30d",
    )

    assert "wildcard" in output.lower() or "*" in output
    assert _authority(enterprise).principals().list() == []
    assert _approval_rows(approvals) == []


# ------------------------------------------------------------- list / show


def _assert_lists_every_field(output: str, record: Any, agent: str) -> None:
    lowered = output.lower()
    for label in ("name", "did", "scope", "agent", "subject", "status", "issuer", "approver"):
        assert label in lowered, f"{label!r} missing from output"
    assert "last use" in lowered
    assert "expir" in lowered
    assert record.name in output
    assert record.did in output
    assert "runs:read" in output
    assert agent in output
    assert record.status in output
    assert record.created_by in output
    if record.approved_by is not None:
        assert record.approved_by in output


def test_list_shows_every_field_and_never_the_secret_or_its_digest(
    deployment: Deployment, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ada = _operator_account(deployment)
    agent = _agent_did()
    _bare_issue(tmp_path, agent, "--subject", "ada@example.com")
    capsys.readouterr()

    _arc("list")

    output = capsys.readouterr().out
    record = _only_principal(deployment)
    _assert_lists_every_field(output, record, agent)
    assert ada.did in output
    _assert_no_secret(output, record)


def test_show_shows_every_field_and_never_the_secret_or_its_digest(
    deployment: Deployment, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _operator_account(deployment)
    agent = _agent_did()
    _bare_issue(tmp_path, agent)
    capsys.readouterr()
    record = _only_principal(deployment)

    _arc("show", record.id)

    output = capsys.readouterr().out
    _assert_lists_every_field(output, record, agent)
    _assert_no_secret(output, record)


def test_show_an_unknown_principal_exits_1(
    deployment: Deployment, capsys: pytest.CaptureFixture[str]
) -> None:
    _operator_account(deployment)
    output = _arc_fails(capsys, "show", "f" * 32)
    assert "f" * 32 in output


# ---------------------------------------------------- revoke -> middleware 401


def _principal_app(deployment: Deployment, tmp_path: Path, tier: str) -> Any:
    """arcui with its principal seam reading the SAME Vault-backed record as the CLI."""
    from packages.arcui.tests.principal_fixtures import build_principal_app

    authority = _authority(deployment)
    rig = build_principal_app(
        tmp_path / "ui",
        principal_store_factory=authority.principals,
        users=authority,
        tier=tier,
    )
    rig.add_probe("/api/workflow-runs/{run_id}", methods=("GET",))
    return rig


def _get_run(rig: Any, bearer: str) -> int:
    from starlette.testclient import TestClient

    # No lifespan (as in the arcui principal tests): only the request path is under test.
    client = TestClient(rig.app, raise_server_exceptions=False)
    response = client.get(
        "/api/workflow-runs/run-1", headers={"Authorization": f"Bearer {bearer}"}
    )
    return int(response.status_code)


def test_revoke_makes_the_next_request_with_the_old_bearer_401(
    deployment: Deployment, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _operator_account(deployment)
    _bare_issue(tmp_path, _agent_did())
    bearer = _issued_bearer(capsys.readouterr().out)
    record = _only_principal(deployment)
    rig = _principal_app(deployment, tmp_path, "personal")
    assert _get_run(rig, bearer) == 200  # the issued credential works before revoke

    _arc("revoke", record.id)

    out = capsys.readouterr().out
    assert record.id in out
    assert _only_principal(deployment).status == "revoked"
    assert _get_run(rig, bearer) == 401
    _arc("list")
    assert "revoked" in capsys.readouterr().out


def test_revoke_an_unknown_principal_exits_1(
    deployment: Deployment, capsys: pytest.CaptureFixture[str]
) -> None:
    _operator_account(deployment)
    output = _arc_fails(capsys, "revoke", "e" * 32)
    assert "e" * 32 in output


def test_a_pending_enterprise_principal_is_refused_by_the_middleware(
    enterprise: Deployment, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The secret is printed at request time, but it unlocks nothing until approved."""
    _operator_account(enterprise)
    _bare_issue(tmp_path, _agent_did(), "--expires", "30d")
    bearer = _issued_bearer(capsys.readouterr().out)

    rig = _principal_app(enterprise, tmp_path, "enterprise")

    assert _get_run(rig, bearer) == 401
