"""P18-2 §8.4 — agents get a short-lived capability handle, never the credential."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from arctrust.audit import AuditEvent
from packages.arcagent.tests.custody_fakes import InterleavingBackend, make_cipher, static_client

from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier
from arcagent.extension.attachment import ProbeResult, ToolResult, ToolSpec
from arcagent.extension.connection_health import StoreHealthReporter
from arcagent.extension.credential_broker import (
    AccessTokenBroker,
    CredentialPlan,
    credential_plan,
)
from arcagent.extension.credentials import RefreshRequest, RenewalPlanner, RenewedCredential
from arcagent.extension.custody import CredentialRowStore
from arcagent.extension.grants import Connection, ConnectionRegistry
from arcagent.extension.manifest import load_manifest
from arcagent.extension.native_attachment import NATIVE_ENTRYPOINT_ATTR
from arcagent.extension.secrets import Secret
from arcagent.modules.connectors.attachments import build_attachment

ACTOR = "did:arc:operator:test"
AGENT, AGENT_DID = "josh", "did:arc:agent:josh"

OAUTH_MANIFEST = """
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

[[secrets]]
name = "api_token"

[oauth]
authorize_url = "https://auth.example/authorize"
token_url = "https://auth.example/token"
provider = "example"
refresh_token_secret = "refresh_token"

[health]
probe = "attachment"
"""

SLACK_MANIFEST = """
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


class ListSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class _Captured:
    def __init__(self, context: dict[str, Any]) -> None:
        self.context = context

    def requirements(self) -> list[Any]:
        return []

    async def probe(self) -> ProbeResult:
        return ProbeResult(reachable=True, detail="ok", tools=[])

    async def describe_tools(self) -> list[ToolSpec]:
        return []

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        raise NotImplementedError


class Provider:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, request: RefreshRequest) -> RenewedCredential:
        self.calls += 1
        return RenewedCredential(
            access_token=Secret(f"from-provider-{self.calls}"), expires_in=3600
        )


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.backend = InterleavingBackend()
        self.sink = ListSink()
        self.arc_dir = tmp_path / "arc"
        self.registry = ConnectionRegistry(self.arc_dir)
        self.rows = CredentialRowStore(self.backend, make_cipher(), sink=self.sink)
        self.provider = Provider()

    async def open(self) -> Any:
        return self.backend

    def broker(self, *, agent: str = AGENT, did: str = AGENT_DID) -> AccessTokenBroker:
        health = StoreHealthReporter(self.open)
        planner = RenewalPlanner(
            rows=self.rows, refresh=self.provider, health=health, owner_id="p", sink=self.sink,
            client=static_client,
        )
        return AccessTokenBroker(
            self.rows,
            registry=lambda: ConnectionRegistry(self.arc_dir),
            renewals=planner,
            health=health,
            sink=self.sink,
            bound_agent=agent,
            bound_did=did,
        )

    def grant(self, connection: str, extension: str, agents: tuple[str, ...]) -> None:
        self.registry.define(
            connection, Connection(extension=extension, approval="auto", agents=agents)
        )

    def reads(self, outcome: str) -> list[AuditEvent]:
        return [
            event
            for event in self.sink.events
            if event.action == "secret.read" and event.outcome == outcome
        ]


@pytest.fixture
async def world(tmp_path: Path) -> World:
    built = World(tmp_path)
    await built.rows.put_fields(
        "blackarc",
        {"app_key": "key", "app_secret": "app-secret-x", "refresh_token": "refresh-x"},
        actor_did=ACTOR,
    )
    await built.rows.put_fields("blackarc", {"api_token": "api-token-x"}, actor_did=ACTOR)
    built.grant("blackarc", "fakebox", (AGENT,))
    return built


def _plan(text: str) -> tuple[Any, CredentialPlan]:
    manifest = load_manifest(text, tier=Tier.PERSONAL)
    return manifest, credential_plan(manifest)


async def test_agent_gets_short_lived_handle_and_never_refresh_token(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    built: list[_Captured] = []
    module = ModuleType("fakebox_entry")
    setattr(
        module,
        NATIVE_ENTRYPOINT_ATTR,
        lambda context: built.append(_Captured(context)) or built[-1],
    )
    monkeypatch.setitem(sys.modules, "fakebox_entry", module)
    manifest, plan = _plan(OAUTH_MANIFEST)
    handle = world.broker().handle("blackarc", agent=AGENT, agent_did=AGENT_DID, plan=plan)

    build_attachment(
        manifest,
        tmp_path,
        {"app_key": Secret("key"), "app_secret": Secret("app-secret-x")},
        connection_id="blackarc",
        credential=handle,
    )

    context = built[0].context
    assert context["credential"] is handle
    assert context["app_key"] == "key"  # visible setting
    for name in ("refresh_token", "app_secret", "api_token"):
        assert name not in context
    rendered = repr(context)
    for value in ("refresh-x", "app-secret-x", "api-token-x"):
        assert value not in rendered
    for withheld in ("refresh_token", "app_key"):
        with pytest.raises(ExtensionError) as caught:
            await handle.field(withheld)
        assert caught.value.code == "CREDENTIAL_FIELD_WITHHELD"
    assert (await handle.field("api_token")).reveal() == "api-token-x"
    assert (await handle.bearer()).reveal() == "from-provider-1"
    assert world.reads("deny")
    audited = repr([event.model_dump() for event in world.sink.events])
    for value in ("refresh-x", "app-secret-x", "api-token-x", "from-provider-1"):
        assert value not in audited


async def test_revoked_grant_stops_a_live_handle(world: World) -> None:
    _, plan = _plan(OAUTH_MANIFEST)
    handle = world.broker().handle("blackarc", agent=AGENT, agent_did=AGENT_DID, plan=plan)
    assert (await handle.bearer()).reveal() == "from-provider-1"

    world.grant("blackarc", "fakebox", ())
    with pytest.raises(ExtensionError) as caught:
        await handle.bearer()
    assert caught.value.code == "CREDENTIAL_NOT_GRANTED"
    assert any(event.extra.get("reason") == "not_granted" for event in world.reads("deny"))
    assert world.provider.calls == 1


async def test_handle_reads_committed_row_not_provider_response(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, plan = _plan(OAUTH_MANIFEST)
    original = world.rows.commit_renewal

    async def commit_other(lease: Any, **kwargs: Any) -> bool:
        kwargs["access_token"] = "committed-token"
        return await original(lease, **kwargs)

    monkeypatch.setattr(world.rows, "commit_renewal", commit_other)
    handle = world.broker().handle("blackarc", agent=AGENT, agent_did=AGENT_DID, plan=plan)
    assert (await handle.bearer()).reveal() == "committed-token"
    # A second call is served from the row, without a provider call.
    assert (await handle.bearer()).reveal() == "committed-token"
    assert world.provider.calls == 1


async def test_invalidate_forces_one_renewal(world: World) -> None:
    _, plan = _plan(OAUTH_MANIFEST)
    handle = world.broker().handle("blackarc", agent=AGENT, agent_did=AGENT_DID, plan=plan)
    assert (await handle.bearer()).reveal() == "from-provider-1"
    await handle.invalidate()
    assert (await handle.bearer()).reveal() == "from-provider-2"
    assert (await handle.bearer()).reveal() == "from-provider-2"
    assert world.provider.calls == 2


async def test_static_bearer_field_is_returned_for_slack(world: World) -> None:
    await world.rows.put_fields("work_slack", {"user_token": "xoxp-123"}, actor_did=ACTOR)
    world.grant("work_slack", "fakeslack", (AGENT,))
    _, plan = _plan(SLACK_MANIFEST)
    assert plan.bearer_field == "user_token" and plan.oauth is None
    handle = world.broker().handle("work_slack", agent=AGENT, agent_did=AGENT_DID, plan=plan)
    assert (await handle.bearer()).reveal() == "xoxp-123"
    assert repr(handle) == "AccessTokenHandle(work_slack)"
    assert world.provider.calls == 0


async def test_broker_issues_only_for_its_own_agent(world: World) -> None:
    _, plan = _plan(OAUTH_MANIFEST)
    broker = world.broker()
    with pytest.raises(ExtensionError) as caught:
        broker.handle("blackarc", agent=AGENT, agent_did="did:arc:agent:forged", plan=plan)
    assert caught.value.code == "CREDENTIAL_NOT_GRANTED"
    with pytest.raises(ExtensionError):
        broker.operator_handle("blackarc", actor_did=ACTOR, plan=plan)


def test_bearer_must_name_a_sensitive_secret() -> None:
    with pytest.raises(ValueError, match="sensitive"):
        load_manifest(
            SLACK_MANIFEST.replace(
                'name = "user_token"\n', 'name = "user_token"\nsensitive = false\n'
            ),
            tier=Tier.PERSONAL,
        )
