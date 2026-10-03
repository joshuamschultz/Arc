"""P18-2F — sealed custody under ``vault_transit``: the Transit row cipher.

The row store, broker and renewer are unchanged; only the cipher differs. These
tests prove the seam end to end: values round-trip through the transit, the raw
row holds ciphertext only, an unreachable transit fails closed with a "wait"
status (never "reconnect"), the cipher runs off the event loop, and the cipher
is chosen by custody in exactly one place.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
from arctrust import FileNotaryTransit, OperatorKey, TransitConnectorCipher
from arctrust.connector_cipher import ConnectorSecretCipher
from arctrust.transit_cipher import TransitUnavailableError
from packages.arcagent.tests.custody_fakes import (
    InterleavingBackend,
    make_cipher,
    static_client,
)

from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier
from arcagent.extension.connection_health import StoreHealthReporter
from arcagent.extension.credential_broker import AccessTokenBroker, credential_plan
from arcagent.extension.credentials import RefreshRequest, RenewalPlanner, RenewedCredential
from arcagent.extension.custody import CREDENTIAL_COLLECTION, CredentialRowStore
from arcagent.extension.custody_select import (
    VAULT_REQUIRED,
    connector_cipher,
    deployment_cipher,
)
from arcagent.extension.grants import Connection, ConnectionRegistry
from arcagent.extension.manifest import load_manifest
from arcagent.extension.secrets import Secret
from arcagent.extension.state import ConnectionRecord, ConnectionStateStore

ACTOR = "did:arc:operator:test"
AGENT, AGENT_DID = "josh", "did:arc:agent:josh"

SLACK = """
[extension]
name = "fakeslack"
version = "1.0.0"
attachment = "native"

[config.native]
entrypoint = "fakeslack_entry"

[[secrets]]
name = "user_token"

[credential]
bearer = "user_token"

[health]
probe = "attachment"
"""

OAUTH = """
[extension]
name = "fakebox"
version = "1.0.0"
attachment = "native"

[config.native]
entrypoint = "fakebox_entry"

[[secrets]]
name = "app_key"
sensitive = false

[[secrets]]
name = "app_secret"

[[secrets]]
name = "refresh_token"

[oauth]
authorize_url = "https://auth.example/authorize"
token_url = "https://auth.example/token"
provider = "example"
refresh_token_secret = "refresh_token"

[health]
probe = "attachment"
"""


class SwitchableTransit:
    """The reference notary, with an off switch and a record of calling threads."""

    def __init__(self, keystore: Path) -> None:
        self._inner = FileNotaryTransit(keystore)
        self.down = False
        self.threads: list[str] = []

    def encrypt(self, key_ref: str, plaintext: bytes, *, aad: bytes) -> str:
        self._gate()
        return self._inner.encrypt(key_ref, plaintext, aad=aad)

    def decrypt(self, key_ref: str, ciphertext: str, *, aad: bytes) -> bytes:
        self._gate()
        return self._inner.decrypt(key_ref, ciphertext, aad=aad)

    def _gate(self) -> None:
        self.threads.append(threading.current_thread().name)
        if self.down:
            raise TransitUnavailableError("transit notary did not answer")


class Provider:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, request: RefreshRequest) -> RenewedCredential:
        self.calls += 1
        return RenewedCredential(access_token=Secret(f"fresh-{self.calls}"), expires_in=3600)


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.backend = InterleavingBackend()
        self.transit = SwitchableTransit(tmp_path / "notary")
        self.rows = CredentialRowStore(self.backend, TransitConnectorCipher(self.transit))
        self.arc_dir = tmp_path / "arc"
        self.provider = Provider()

    async def open(self) -> Any:
        return self.backend

    async def grant(self, connection: str, extension: str) -> None:
        ConnectionRegistry(self.arc_dir).define(
            connection, Connection(extension=extension, approval="auto", agents=(AGENT,))
        )
        await ConnectionStateStore(self.backend).create(
            ConnectionRecord(connection=connection, custody="arc"), actor_did=ACTOR
        )

    def broker(self) -> AccessTokenBroker:
        health = StoreHealthReporter(self.open)
        planner = RenewalPlanner(
            rows=self.rows,
            refresh=self.provider,
            health=health,
            owner_id="p",
            client=static_client,
        )
        return AccessTokenBroker(
            self.rows,
            registry=lambda: ConnectionRegistry(self.arc_dir),
            renewals=planner,
            health=health,
            bound_agent=AGENT,
            bound_did=AGENT_DID,
        )

    async def record(self, connection: str) -> Any:
        return await ConnectionStateStore(self.backend).get(connection)


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def _plan(text: str) -> Any:
    return credential_plan(load_manifest(text, tier=Tier.PERSONAL))


async def test_transit_rows_round_trip_and_hold_ciphertext_only(world: World) -> None:
    await world.rows.put_fields("work_slack", {"user_token": "xoxp-777"}, actor_did=ACTOR)
    await world.grant("work_slack", "fakeslack")
    handle = world.broker().handle(
        "work_slack", agent=AGENT, agent_did=AGENT_DID, plan=_plan(SLACK)
    )

    assert (await handle.bearer()).reveal() == "xoxp-777"
    raw = await world.backend.mutable_query(CREDENTIAL_COLLECTION)
    assert raw[0]["cipher"] == "transit1"
    assert raw[0]["fields"]["user_token"]["sealed"].startswith("v1.tr.notary:v1:")
    assert "xoxp-777" not in repr(raw)


async def test_cipher_calls_run_off_the_event_loop(world: World) -> None:
    await world.rows.put_fields("work_slack", {"user_token": "xoxp-1"}, actor_did=ACTOR)
    row = await world.rows.read("work_slack")
    assert row is not None
    await world.rows.open_field(row, "user_token")
    assert world.transit.threads
    assert threading.main_thread().name not in world.transit.threads


async def test_transit_unreachable_fails_closed_and_the_card_says_wait(world: World) -> None:
    await world.rows.put_fields("work_slack", {"user_token": "xoxp-777"}, actor_did=ACTOR)
    await world.grant("work_slack", "fakeslack")
    handle = world.broker().handle(
        "work_slack", agent=AGENT, agent_did=AGENT_DID, plan=_plan(SLACK)
    )
    world.transit.down = True

    with pytest.raises(ExtensionError) as caught:
        await handle.bearer()

    assert caught.value.code == "CREDENTIAL_CUSTODY_UNAVAILABLE"
    assert "xoxp-777" not in caught.value.message
    record = await world.record("work_slack")
    assert record is not None
    assert (record.status, record.reason_code, record.action) == (
        "error",
        "custody_unavailable",
        "wait",
    )
    assert "vault" in (record.reason_text or "")


async def test_transit_unreachable_on_write_stores_nothing(world: World) -> None:
    world.transit.down = True
    with pytest.raises(ExtensionError) as caught:
        await world.rows.put_fields("work_slack", {"user_token": "xoxp-1"}, actor_did=ACTOR)
    assert caught.value.code == "CREDENTIAL_CUSTODY_UNAVAILABLE"
    assert await world.backend.mutable_query(CREDENTIAL_COLLECTION) == []


async def test_renewal_with_transit_down_never_calls_the_provider(world: World) -> None:
    await world.rows.put_fields(
        "blackarc",
        {"app_key": "key", "app_secret": "secret", "refresh_token": "refresh"},
        actor_did=ACTOR,
    )
    await world.grant("blackarc", "fakebox")
    handle = world.broker().handle("blackarc", agent=AGENT, agent_did=AGENT_DID, plan=_plan(OAUTH))
    world.transit.down = True

    with pytest.raises(ExtensionError):
        await handle.bearer()

    assert world.provider.calls == 0
    record = await world.record("blackarc")
    assert record is not None and record.reason_code == "custody_unavailable"
    assert record.status != "needs_you"

    world.transit.down = False
    assert (await handle.bearer()).reveal() == "fresh-1"


async def test_in_process_row_under_transit_names_the_reseal_verb(world: World) -> None:
    legacy = CredentialRowStore(world.backend, make_cipher())
    await legacy.put_fields("work_slack", {"user_token": "xoxp-1"}, actor_did=ACTOR)
    row = await world.rows.read("work_slack")
    assert row is not None

    with pytest.raises(ExtensionError) as caught:
        await world.rows.open_field(row, "user_token")

    assert caught.value.code == "CREDENTIAL_UNREADABLE"
    assert "moved into the vault" in caught.value.message
    assert "arc " not in caught.value.message


def test_custody_selects_the_cipher_in_one_place(tmp_path: Path) -> None:
    transit = FileNotaryTransit(tmp_path / "notary")
    key = OperatorKey.load(tmp_path / "op" / "operator.key", generate_if_absent=True)

    chosen = connector_cipher(custody="vault_transit", operator_key=key, transit=transit)
    assert isinstance(chosen, TransitConnectorCipher)
    assert chosen.kind == "transit1"
    in_process = connector_cipher(custody="in_process", operator_key=key, transit=transit)
    assert isinstance(in_process, ConnectorSecretCipher)
    with pytest.raises(ExtensionError) as caught:
        connector_cipher(custody="vault_transit", operator_key=key, transit=None)
    assert caught.value.code == VAULT_REQUIRED


def _deployment(tmp_path: Path, *, provision: bool) -> Path:
    arc_dir = tmp_path / "arc"
    keystore = tmp_path / "notary"
    config = arc_dir / "config"
    config.mkdir(parents=True)
    (config / "arcagent.toml").write_text(
        f'[security]\ntier = "enterprise"\nnotary_keystore = "{keystore}"\n', encoding="utf-8"
    )
    if provision:
        FileNotaryTransit.provision(keystore, "operator", b"\x07" * 32)
    return arc_dir


def test_enterprise_deployment_seals_under_transit_by_default(tmp_path: Path) -> None:
    arc_dir = _deployment(tmp_path, provision=True)
    cipher = deployment_cipher(arc_dir, tier=Tier.ENTERPRISE)
    assert cipher.kind == "transit1"
    assert not list(arc_dir.rglob("operator.key"))


def test_enterprise_deployment_without_a_serving_transit_refuses(tmp_path: Path) -> None:
    arc_dir = _deployment(tmp_path, provision=False)
    with pytest.raises(ExtensionError) as caught:
        deployment_cipher(arc_dir, tier=Tier.ENTERPRISE)
    assert caught.value.code == VAULT_REQUIRED
