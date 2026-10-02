"""``Connections.check_health`` — the one place that decides "does this connection work" (P18-1, 5.3).

A real bundle on disk, a real :class:`Connections`, a real in-memory arcstore. Only the
attachment (the provider wire) is a double, so a probe that ran the wrong thing, or
wrote a record nobody reads, cannot pass.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from arcstore.backends.memory import FakeBackend
from arctrust import causal
from arctrust.audit import AuditEvent

from arcagent.connection_catalog import AuditChain
from arcagent.connections import Connections
from arcagent.extension.attachment import ProbeResult, ToolOutcome, ToolResult, ToolSpec
from arcagent.extension.connection_health import PROBE_DID
from arcagent.extension.secrets import SecretRef
from arcagent.extension.state import ConnectionStateStore

_EXTENSION = "acme_health"
_INSTANCE = "work"

_MANIFEST = """
[extension]
name = "acme_health"
version = "1.0.0"
display_name = "Acme"
attachment = "native"

[config.native]
entrypoint = "acme_attachment"

[[secrets]]
name = "api_token"
prompt = "Acme API token."

{health}

[tools]
allow = ["x_ping"]

[[tools.declared]]
name = "x_ping"
description = "Ping Acme."
classification = "read_only"
"""

_HOST = """
[[host_requires]]
name = "acmecli"
instruction = "install acmecli"
"""


class _Recording:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class _Provider:
    """The provider wire, with a script of how it answers."""

    def __init__(self) -> None:
        self.probe_calls = 0
        self.invoked: list[tuple[str, dict[str, Any]]] = []
        self.reachable = True
        self.detail = "ok"
        self.tool_outcome = ToolOutcome.OK
        self.tool_text = ""
        self.delay = 0.0
        self.build_error: Exception | None = None


class _Attachment:
    def __init__(self, provider: _Provider) -> None:
        self._provider = provider

    def requirements(self) -> list[Any]:
        return []

    async def probe(self) -> ProbeResult:
        self._provider.probe_calls += 1
        await asyncio.sleep(self._provider.delay)
        return ProbeResult(
            reachable=self._provider.reachable,
            tools=await self.describe_tools(),
            detail=self._provider.detail,
        )

    async def describe_tools(self) -> list[ToolSpec]:
        return [ToolSpec(name="x_ping", description="Ping Acme.")]

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        self._provider.invoked.append((tool, args))
        return ToolResult(
            tool=tool, outcome=self._provider.tool_outcome, content=self._provider.tool_text
        )


class _World:
    def __init__(self, tmp_path: Path, provider: _Provider, health: str) -> None:
        self.provider = provider
        self.sink = _Recording()
        self.backend = FakeBackend()
        arc_dir = tmp_path / "arc"
        bundle = arc_dir / "extensions" / _EXTENSION
        bundle.mkdir(parents=True)
        (bundle / "extension.toml").write_text(_MANIFEST.format(health=health), encoding="utf-8")

        async def open_backend() -> FakeBackend:
            return self.backend

        def factory(_manifest: Any, _bundle: Any, _secrets: Any) -> _Attachment:
            if provider.build_error is not None:
                raise provider.build_error
            return _Attachment(provider)

        self.connections = Connections.for_deployment(
            arc_dir=arc_dir,
            data_dir=tmp_path / "data",
            state_opener=open_backend,
            audit=AuditChain.held(self.sink),
            attachment_factory=factory,
        )

    async def install(self) -> None:
        plan = self.connections.plan(_EXTENSION, _INSTANCE)
        await self.connections.install(plan, {"api_token": "tok-1"})

    async def record(self) -> Any:
        return await ConnectionStateStore(self.backend).get(_INSTANCE)


@pytest.fixture
def provider() -> _Provider:
    return _Provider()


def _world(tmp_path: Path, provider: _Provider, health: str) -> _World:
    return _World(tmp_path, provider, health)


async def test_probe_uses_manifest_health_tool_and_persists_record(
    tmp_path: Path, provider: _Provider
) -> None:
    world = _world(tmp_path, provider, '[health]\nprobe = "tool:x_ping"\nargs = { limit = "1" }')
    await world.install()
    provider.invoked.clear()
    provider.probe_calls = 0
    world.sink.events.clear()

    with causal.bind(causal.root("connector_probe", PROBE_DID)):
        record = await world.connections.check_health(_INSTANCE, checked_by=PROBE_DID)

    assert provider.invoked == [("x_ping", {"limit": "1"})]
    assert provider.probe_calls == 0, "a tool probe must not run the attachment probe too"
    assert record.status == "healthy"
    assert record.last_checked_at is not None
    assert record.checked_by == PROBE_DID
    checked = [e for e in world.sink.events if e.action == "connection.health.checked"]
    assert [(e.actor_did, e.extra["ok"], e.extra["probe"]) for e in checked] == [
        (PROBE_DID, True, "tool:x_ping")
    ]


async def test_a_failing_tool_probe_is_classified_from_the_provider_text(
    tmp_path: Path, provider: _Provider
) -> None:
    world = _world(tmp_path, provider, '[health]\nprobe = "tool:x_ping"')
    await world.install()
    provider.tool_outcome = ToolOutcome.ERROR
    provider.tool_text = "401 Unauthorized: token has been expired or revoked"

    record = await world.connections.check_health(_INSTANCE, checked_by=PROBE_DID)

    assert (record.status, record.action, record.reason_code) == (
        "needs_you",
        "reconnect",
        "auth_required",
    )
    assert record.reason_text == "Acme sign-in expired or was revoked"
    assert record.notice_seq == record.transition_seq == 2


async def test_attachment_probe_reports_the_providers_detail_when_unreachable(
    tmp_path: Path, provider: _Provider
) -> None:
    world = _world(tmp_path, provider, '[health]\nprobe = "attachment"')
    await world.install()
    provider.reachable = False
    provider.detail = "connection refused"

    record = await world.connections.check_health(_INSTANCE, checked_by=PROBE_DID)

    assert provider.probe_calls >= 1
    assert record.status == "healthy", "one counted failure on a healthy connection is a blip"
    assert record.consecutive_failures == 1
    assert record.reason_text is None


async def test_probe_timeout_is_counted_provider_unavailable_not_terminal(
    tmp_path: Path, provider: _Provider
) -> None:
    world = _world(tmp_path, provider, '[health]\nprobe = "attachment"')
    await world.install()
    provider.delay = 30.0

    record = await world.connections.check_health(_INSTANCE, checked_by=PROBE_DID, timeout=0.05)

    assert record.status == "healthy"
    assert record.consecutive_failures == 1


async def test_a_missing_credential_is_terminal(tmp_path: Path, provider: _Provider) -> None:
    world = _world(tmp_path, provider, '[health]\nprobe = "attachment"')
    await world.install()
    with world.connections._audit.open() as sink:
        await world.connections._store(sink).delete(
            SecretRef(connection=_INSTANCE, field="api_token"), caller_did="did:arc:test"
        )

    record = await world.connections.check_health(_INSTANCE, checked_by=PROBE_DID)

    assert (record.status, record.reason_code) == ("needs_you", "credential_missing")


async def test_unsatisfied_host_is_host_missing_without_spawning(
    tmp_path: Path, provider: _Provider
) -> None:
    world = _world(tmp_path, provider, '[health]\nprobe = "attachment"')
    await world.install()
    manifest = world.connections.world.extension_roots[0] / _EXTENSION / "extension.toml"
    manifest.write_text(
        manifest.read_text() + _HOST.replace("acmecli", "definitely-not-on-path-1")
    )
    provider.probe_calls = 0

    record = await world.connections.check_health(_INSTANCE, checked_by=PROBE_DID)

    assert provider.probe_calls == 0, "a missing binary must not be probed"
    assert (record.status, record.action, record.reason_code) == (
        "needs_you",
        "install_host",
        "host_missing",
    )


async def test_install_writes_unknown_then_operator_check_without_notice(
    tmp_path: Path, provider: _Provider
) -> None:
    world = _world(tmp_path, provider, '[health]\nprobe = "attachment"')

    await world.install()

    record = await world.record()
    assert record.status == "healthy"
    assert record.notice_seq == 0
    assert record.custody == "arc"
    assert record.checked_by is not None


async def test_an_operator_check_that_fails_sends_no_notice(
    tmp_path: Path, provider: _Provider
) -> None:
    world = _world(tmp_path, provider, '[health]\nprobe = "tool:x_ping"')
    await world.install()
    provider.tool_outcome = ToolOutcome.ERROR
    provider.tool_text = "401 unauthorized"

    record = await world.connections.check_health(
        _INSTANCE, checked_by="did:arc:operator", source="operator"
    )

    assert record.status == "needs_you"
    assert record.notice_seq == 0, "the operator is looking at the card; no notice"


async def test_approve_clears_a_changed_contract_and_a_probe_does_not(
    tmp_path: Path, provider: _Provider
) -> None:
    from arcagent.extension.connection_health import ConnectionHealthAuthority, HealthSignal

    world = _world(tmp_path, provider, '[health]\nprobe = "attachment"')
    await world.install()
    authority = ConnectionHealthAuthority(ConnectionStateStore(world.backend))
    await authority.record(
        _INSTANCE,
        HealthSignal(
            ok=False,
            source="contract",
            checked_by="did:arc:arcagent:contract-ledger",
            reason_code="contract_changed",
            detail="1",
        ),
    )

    after_probe = await world.connections.check_health(_INSTANCE, checked_by=PROBE_DID)
    assert (after_probe.status, after_probe.action) == ("needs_you", "approve")

    await world.connections.approve(_INSTANCE)
    assert (await world.record()).status == "healthy"


async def test_a_connection_with_no_record_gets_one_so_the_loop_can_see_it(
    tmp_path: Path, provider: _Provider
) -> None:
    world = _world(tmp_path, provider, '[health]\nprobe = "attachment"')
    await world.install()
    await ConnectionStateStore(world.backend).forget(_INSTANCE, actor_did="did:arc:test")
    assert await world.record() is None

    await world.connections.ensure_health_records()

    assert (await world.record()).status == "unknown"
