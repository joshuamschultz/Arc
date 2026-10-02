"""P18-2 §8.7 — no connector credential is ever written to disk in plaintext.

(a) The legacy plaintext file is named by exactly one module, the migrator (plus the
    home-layout move that relocates a flat-layout copy to where the migrator reads it).
(b) A full migrate / connect / re-auth / health-check cycle in a temporary
    deployment leaves no file under the deployment root holding a secret value.
(c) The deleted plaintext and never-selected vault backends are gone for good.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Any

import arcagent
import pytest
from arcagent.extension.attachment import ProbeResult, ToolSpec
from arcstore.backends.memory import FakeBackend
from arctrust.connector_cipher import ConnectorSecretCipher
from arctrust.paths import config_file

REPO = Path(__file__).resolve().parents[2]
LEGACY = "connections.env"
#: Modules allowed to spell the legacy filename, each with its reason.
ALLOWED = {
    REPO / "packages/arcagent/src/arcagent/extension/custody_migrate.py": "the one-time migrator",
    REPO / "packages/arctrust/src/arctrust/home_migration.py": (
        "moves a flat-layout copy into config/, where the migrator reads it"
    ),
}
SECRETS = ("xoxp-arch-secret-1", "xoxp-arch-secret-2")


def test_only_the_migrator_names_the_legacy_file() -> None:
    offenders = [
        str(path.relative_to(REPO))
        for path in REPO.glob("packages/*/src/**/*.py")
        if path not in ALLOWED and LEGACY in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


@pytest.mark.parametrize(
    ("module", "name"),
    [
        ("arcagent.extension.secrets", "select_secret_backend"),
        ("arcagent.extension.secrets", "LocalFileSecretBackend"),
        ("arcagent.extension.secrets", "VaultSecretBackend"),
        ("arcagent.extension.secrets", "WritableVault"),
        ("arcagent.modules.connectors.install", "connector_env_file"),
    ],
)
def test_plaintext_backends_are_not_importable(module: str, name: str) -> None:
    assert not hasattr(importlib.import_module(module), name)


class _FakeAttachment:
    def requirements(self) -> list[Any]:
        return []

    async def probe(self) -> ProbeResult:
        return ProbeResult(reachable=True, detail="ok", tools=[])

    async def describe_tools(self) -> list[ToolSpec]:
        return []

    async def invoke(self, tool: str, args: dict[str, Any]) -> Any:
        raise NotImplementedError


def _factory(*_args: Any, **_kwargs: Any) -> _FakeAttachment:
    return _FakeAttachment()


async def test_full_cycle_leaves_no_secret_on_disk(tmp_path: Path) -> None:
    arc_dir = tmp_path / "arc"
    backend = FakeBackend()

    async def opener() -> Any:
        return backend

    world = arcagent.resolve_deployment(arc_dir=arc_dir, extensions_root=REPO / "extensions")
    connections = arcagent.Connections(
        world,
        state_opener=opener,
        attachment_factory=_factory,
        credential_cipher=ConnectorSecretCipher(b"\x05" * 32),
    )
    connections.registry.define(
        "work_slack", arcagent.Connection(extension="slack", approval="auto", agents=())
    )
    legacy = config_file(LEGACY, arc_dir)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(legacy), os.O_WRONLY | os.O_CREAT, 0o600)
    os.write(fd, f"ARC_SECRET_WORK_SLACK_USER_TOKEN={SECRETS[0]}\n".encode())
    os.close(fd)

    await connections.migrate_secrets()
    plan = connections.plan_for("work_slack")
    await connections.reauth(plan, {"user_token": SECRETS[1]})
    await connections.check_health("work_slack", checked_by="did:arc:test")

    leaked = [
        str(path)
        for path in arc_dir.rglob("*")
        if path.is_file() and any(secret.encode() in path.read_bytes() for secret in SECRETS)
    ]
    assert leaked == []
    assert not legacy.exists()
