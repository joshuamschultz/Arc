"""`arc accounts enroll`: signed, anchored, 0600, audited, and never prints the token."""

from __future__ import annotations

import io
import json
import stat
from pathlib import Path

import pytest
from arctrust.paths import config_file
from packages.arccli.tests.accounts_support import (
    DEPLOYMENT,
    TENANT,
    enroll_args,
    run_enroll,
)
from packages.arccli.tests.fake_vault import ADMIN_TOKEN, FakeVault

from arccli.commands.accounts import accounts_handler


@pytest.fixture(autouse=True)
def _isolated_arc_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Own Arc home per test, even in the cross-package battery (no arccli conftest)."""
    monkeypatch.delenv("ARC_TEAM_ROOT", raising=False)
    monkeypatch.delenv("ARCSTORE_DATA_DIR", raising=False)
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc-home"))


@pytest.fixture
def vault(tmp_path: Path):
    with FakeVault(TENANT, DEPLOYMENT, tmp_path) as running:
        running.mint_admin()
        yield running


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_enroll_writes_private_signed_files_and_prints_the_block(
    vault, monkeypatch, capsys: pytest.CaptureFixture[str]
):
    run_enroll(vault, monkeypatch)

    out = capsys.readouterr()
    assert ADMIN_TOKEN not in out.out + out.err
    assert "[security.accounts]" in out.out
    assert "issuer_secret = " in out.out and 'credential:arc-accounts-issuer"' in out.out
    config = config_file("accounts-authority.json")
    grants = config_file("accounts-grants.json")
    assert _mode(config) == _mode(grants) == 0o600
    envelope = json.loads(config.read_text())
    assert envelope["config"]["revision"] == 1
    signed = json.loads(grants.read_text())
    assert set(signed) == {"issuer", "cipher", "anchor"}
    for capability, item in signed.items():
        assert item["grant"]["subject"] == f"arc-{TENANT}-{DEPLOYMENT}-{capability}"
        assert item["grant"]["config_revision"] == 1
    head = vault.kv["config"][-1]
    assert head["scope"] == f"{vault.anchor}/config"


def test_enroll_audits_every_step_on_the_operator_chain(vault, monkeypatch):
    from arcstore import resolve_data_dir

    run_enroll(vault, monkeypatch)

    chain = resolve_data_dir(None) / "worm"
    text = "".join(path.read_text() for path in chain.glob("*.jsonl"))
    for step in ("start", "config_anchor", "config_file", "grants_file", "users_bootstrap"):
        assert f"accounts.enroll.{step}" in text
    assert ADMIN_TOKEN not in text


def test_a_second_enroll_needs_rotate_and_rotate_issues_the_next_revision(vault, monkeypatch):
    run_enroll(vault, monkeypatch)
    with pytest.raises(SystemExit):
        run_enroll(vault, monkeypatch)

    run_enroll(vault, monkeypatch, "--rotate")

    assert (
        json.loads(config_file("accounts-authority.json").read_text())["config"]["revision"] == 2
    )
    assert len(vault.kv["config"]) == 2
    grants = json.loads(config_file("accounts-grants.json").read_text())
    assert {g["grant"]["config_revision"] for g in grants.values()} == {2}


def test_rotate_before_enrollment_is_refused(vault, monkeypatch):
    with pytest.raises(SystemExit):
        run_enroll(vault, monkeypatch, "--rotate")


def test_enroll_refuses_without_the_token_flag(vault, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(ADMIN_TOKEN))
    args = [a for a in enroll_args(vault, vault.cert_path) if a != "--admin-token-stdin"]
    with pytest.raises(SystemExit):
        accounts_handler(args)
    assert "config" not in vault.kv


def test_an_unauthorized_admin_token_writes_nothing(vault, monkeypatch):
    with pytest.raises(SystemExit):
        run_enroll(vault, monkeypatch, token="not-an-admin-token")
    assert not config_file("accounts-authority.json").exists()
    assert not config_file("accounts-grants.json").exists()


def test_grant_days_sets_the_expiry(vault, monkeypatch):
    import time

    run_enroll(vault, monkeypatch, "--grant-days", "30")
    grants = json.loads(config_file("accounts-grants.json").read_text())
    expiry = next(iter(grants.values()))["grant"]["expires_at"]
    assert abs(expiry - (time.time() + 30 * 86_400)) < 60
