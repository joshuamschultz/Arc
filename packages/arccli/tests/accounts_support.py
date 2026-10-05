"""A fully enrolled account-authority deployment against :class:`FakeVault`.

Enrollment runs through the real ``arc accounts enroll`` command; the machine
``[security.accounts]`` block is written the way an operator would paste it.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from arctrust import AuditEvent
from arctrust.paths import config_file
from packages.arccli.tests.fake_vault import ADMIN_TOKEN, FakeVault

from arccli.commands.accounts import accounts_handler

TENANT = "acme"
DEPLOYMENT = "dgx"
CAPABILITIES = ("config-reader", "issuer", "cipher", "anchor")


class RecordingSink:
    """Audit chain stand-in: both append paths record, and either can be made to fail."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []
        self.fail = False

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)

    def write_durable(self, event: AuditEvent) -> None:
        if self.fail:
            raise OSError("disk full")
        self.events.append(event)


@dataclass
class Deployment:
    vault: FakeVault
    home: Path
    users_path: Path

    @property
    def grants_file(self) -> Path:
        return config_file("accounts-grants.json")

    @property
    def authority_config(self) -> Path:
        return config_file("accounts-authority.json")


def enroll_args(vault: FakeVault, ca_file: Path, *extra: str) -> list[str]:
    return [
        "enroll",
        "--vault-url",
        vault.url,
        "--ca-file",
        str(ca_file),
        "--deployment-id",
        DEPLOYMENT,
        "--tenant-id",
        TENANT,
        "--admin-token-stdin",
        *extra,
    ]


def run_enroll(
    vault: FakeVault, monkeypatch: pytest.MonkeyPatch, *extra: str, token: str = ADMIN_TOKEN
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(token))
    accounts_handler(enroll_args(vault, vault.cert_path, *extra))


def _write_accounts_block(vault: FakeVault, home: Path) -> None:
    lines = [
        "[security.accounts]",
        f'vault_url = "{vault.url}"',
        f'ca_file = "{vault.cert_path}"',
        f'deployment_id = "{DEPLOYMENT}"',
        f'tenant_id = "{TENANT}"',
        f'authority_config = "{config_file("accounts-authority.json")}"',
        f'grants_file = "{config_file("accounts-grants.json")}"',
    ]
    for capability in CAPABILITIES:
        role_id, secret_id = vault.add_role(capability)
        secret_file = home / f"{capability}.secret"
        secret_file.write_text(secret_id)
        secret_file.chmod(0o600)
        key = capability.replace("-", "_")
        lines.append(f'{key}_role_id = "{role_id}"')
        lines.append(f'{key}_secret = "file:{secret_file}"')
    target = config_file("arcagent.toml")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n")


@contextmanager
def enrolled_deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Deployment]:
    home = tmp_path / "secrets"
    home.mkdir()
    with FakeVault(TENANT, DEPLOYMENT, home) as vault:
        vault.mint_admin()
        run_enroll(vault, monkeypatch)
        _write_accounts_block(vault, home)
        yield Deployment(vault, home, tmp_path / "users" / "users.json")
