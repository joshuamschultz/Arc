"""A connection is granted to agents, and an ungranted agent gets nothing.

The model: a connection is a connected account defined ONCE for the deployment —
one extension, one instance name, one credential, one host setup. A grant is
``(connection -> agent)``. An agent holding a grant gets that connection's verbs
in its registry; an agent without one gets no verb and no path to the credential.

Every assertion here is taken from a **real** :class:`~arcagent.core.tool_registry.
ToolRegistry` after starting the **real**
:class:`~arcagent.modules.connectors.capabilities.Connectors` capability the way a
running agent starts it — not from a config parser and not from the registry file.
That is deliberate. Six defects shipped on this feature in two days and every one
of them passed a unit test: each was a component that worked alone and a seam
nobody exercised end to end. "The grant is written to the file" and "the ungranted
agent has no tools" are two different claims, and only the second one is the
security property.

The four agents are not decoration either. The operator's ask was "maybe 2 agents
can access jira and 2 don't", so the test that matters is the one where granted
and ungranted agents come out of the same deployment, the same registry file, and
the same credential — and land in different places.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from arcstore.backends.memory import FakeBackend
from arctrust.audit import AuditEvent
from arctrust.paths import arc_team, config_file
from arctrust.signer import InProcessSigner
from nacl.signing import SigningKey

from arcagent.connections import (
    PLAN_TIER_TOO_LOW,
    TIER_WOULD_RISE,
    AuditChain,
    Connections,
)
from arcagent.core.config import ToolConfig, ToolsConfig
from arcagent.core.errors import ExtensionError
from arcagent.core.module_bus import ModuleBus
from arcagent.core.tier import Tier
from arcagent.core.tool_registry import ToolRegistry
from arcagent.extension.grants import NO_SUCH_CONNECTION, Connection, ConnectionRegistry
from arcagent.extension.secrets import SecretRef
from arcagent.modules.connectors import _runtime
from arcagent.modules.connectors.capabilities import Connectors
from arcagent.modules.connectors.install import connector_env_file
from arcagent.tools.human_gate import HumanGate

_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "reference_extension"

_BUNDLE = "reference_service"
_CONNECTION = "primary"
_FIELD = "reference_token"
_TOKEN = "one-account-one-token"

#: The verbs the reference bundle declares. ``reference_store`` carries
#: ``network_egress`` on purpose: a grant that only ever handed over read-only
#: verbs would prove nothing about the ones that can send data out.
_SERVED = ("reference_echo", "reference_store")

#: Four agents out of one deployment. Two hold the grant, two do not — the exact
#: shape the operator described.
_GRANTED = ("coder", "marketer")
_UNGRANTED = ("trader", "sales")


class _RecordingSink:
    """A real audit sink that keeps events. ``write`` only — the arctrust protocol."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def targets(self, action: str) -> list[str]:
        return [event.target for event in self.events if event.action == action]

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


@pytest.fixture(autouse=True)
def _reset_runtime() -> Iterator[None]:
    _runtime.reset()
    yield
    _runtime.reset()


class _Deployment:
    """One arc dir, one bundle root, one data dir, and four agent directories."""

    def __init__(self, tmp_path: Path) -> None:
        self.arc_dir = tmp_path / "arc"
        self.arc_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = tmp_path / "data"
        self.root = tmp_path / "extensions"
        self.root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            _FIXTURE_DIR, self.root / _BUNDLE, ignore=shutil.ignore_patterns("__pycache__")
        )
        # The real fleet layout: agents live in the deployment's team root, which
        # is where `arctui.roster` and the connection seam both look for one's tier.
        self.agents_dir = arc_team(base=self.arc_dir)
        for name in (*_GRANTED, *_UNGRANTED):
            self.agent_dir(name)
        self.sink = _RecordingSink()
        self.arcstore_backend = FakeBackend()

    async def open_arcstore(self) -> FakeBackend:
        return self.arcstore_backend

    def agent_dir(self, agent: str) -> Path:
        """Create (once) and return one real agent directory with its own DID."""
        directory = self.agents_dir / agent
        if not directory.is_dir():
            workspace = directory / "workspace"
            workspace.mkdir(parents=True, exist_ok=True)
            (directory / "arcagent.toml").write_text(
                "[agent]\n"
                f'name = "{agent}"\n'
                'org = "testorg"\n'
                'type = "executor"\n'
                f'workspace = "{workspace}"\n\n'
                "[llm]\n"
                'model = "test/model"\n\n'
                "[identity]\n"
                f'did = "{self.did(agent)}"\n\n'
                "[security]\n"
                'tier = "personal"\n',
                encoding="utf-8",
            )
        return directory

    @staticmethod
    def did(agent: str) -> str:
        return f"did:arc:testorg:executor/{agent}"

    def connections(self) -> Connections:
        """The deployment façade every surface drives, pointed inside the test tree."""
        return Connections.for_deployment(
            arc_dir=self.arc_dir,
            data_dir=self.data_dir,
            extensions_root=self.root,
            audit=AuditChain.held(self.sink),
            state_opener=self.open_arcstore,
        )

    async def connect(self, *, agents: tuple[str, ...] = _GRANTED) -> None:
        """Connect the account once, granting it to ``agents`` in the same act."""
        connections = self.connections()
        plan = connections.plan(_BUNDLE, _CONNECTION)
        await connections.install(plan, {_FIELD: _TOKEN}, agents=agents)

    async def start_agent(self, agent: str, *, sink: _RecordingSink | None = None) -> ToolRegistry:
        registry, _ = await self.start_running_agent(agent, sink=sink)
        return registry

    async def start_running_agent(
        self, agent: str, *, sink: _RecordingSink | None = None, with_arcstore: bool = True
    ) -> tuple[ToolRegistry, Connectors]:
        """Start one agent's connectors capability exactly as the agent starts it."""
        registry = ToolRegistry(
            config=ToolsConfig(policy=ToolConfig()),
            bus=ModuleBus(),
            telemetry=MagicMock(),
            human_gate=_gate(self.did(agent)),
        )
        _runtime.configure(
            config={
                "data_dir": str(self.data_dir),
                "extensions_root": str(self.root),
                "arc_dir": str(self.arc_dir),
            },
            telemetry=_telemetry(sink),
            workspace=self.agent_dir(agent) / "workspace",
            identity=_identity(self.did(agent)),
            config_path=self.agent_dir(agent) / "arcagent.toml",
            tool_registry=registry,
            tier="personal",
            human_gate=_gate(self.did(agent)),
            arcstore_opener=self.open_arcstore if with_arcstore else None,
        )
        capability = Connectors()
        await capability.setup(None)
        return registry, capability

    def registry_file(self) -> ConnectionRegistry:
        return ConnectionRegistry(self.arc_dir)


def _identity(did: str) -> Any:
    identity = MagicMock()
    identity.did = did
    return identity


def _gate(did: str) -> HumanGate:
    return HumanGate(
        operator_signer=InProcessSigner(bytes(SigningKey.generate())),
        agent_did=did,
        tier="personal",
    )


def _telemetry(sink: _RecordingSink | None) -> Any:
    """Adapt a recording sink to the ``telemetry.audit_event`` the module is handed.

    The module wraps telemetry in its own sink adapter, so a test that wants the
    events the module emits has to arrive through the same door the agent does.
    """
    if sink is None:
        return None
    telemetry = MagicMock()
    telemetry.audit_event.side_effect = lambda action, payload: sink.write(
        AuditEvent(
            actor_did=str(payload.get("actor", "")),
            action=action,
            target=str(payload.get("target", "")),
            outcome=str(payload.get("outcome", "")),
        )
    )
    return telemetry


@pytest.fixture
def deployment(tmp_path: Path) -> _Deployment:
    return _Deployment(tmp_path)


# --- the security property ----------------------------------------------------


async def test_exactly_the_granted_agents_serve_the_connection(
    deployment: _Deployment,
) -> None:
    """Four agents, one connection, two grants. Two get the verbs; two get nothing.

    Asserted on each agent's own real ``ToolRegistry`` after a real capability
    start, because the registry is what the model reads its catalog from. A test
    that read the grant back out of the TOML file would have passed on every one
    of the six defects this feature has already shipped.
    """
    await deployment.connect()

    for agent in _GRANTED:
        registry = await deployment.start_agent(agent)
        for tool in _SERVED:
            assert tool in registry.tools, (
                f"{agent} holds a grant for {_CONNECTION!r} and did not get {tool!r} — "
                f"it serves {sorted(registry.tools)}"
            )

    for agent in _UNGRANTED:
        registry = await deployment.start_agent(agent)
        for tool in _SERVED:
            assert tool not in registry.tools, (
                f"{agent} holds NO grant for {_CONNECTION!r} and was served {tool!r}"
            )


async def test_an_ungranted_agent_never_reads_the_credential(
    deployment: _Deployment,
) -> None:
    """Deny by default reaches the credential, not just the tool list.

    Measured where a credential is actually read: the secret store audits every
    read, so an ungranted agent that touched this connection's credential would
    leave a ``secret.read`` event naming it. Zero events is the claim; an empty
    tool registry alone would not distinguish "was refused" from "read the token
    and then failed to attach".
    """
    await deployment.connect()
    granted_sink, ungranted_sink = _RecordingSink(), _RecordingSink()

    await deployment.start_agent(_GRANTED[0], sink=granted_sink)
    registry = await deployment.start_agent(_UNGRANTED[0], sink=ungranted_sink)

    # The control. Without it the assertion below passes on a deployment where
    # nothing reads a credential at all, which is a test that cannot fail.
    assert granted_sink.targets("secret.read"), (
        "a granted agent read no credential — this test is not measuring anything"
    )
    assert not any(tool in registry.tools for tool in _SERVED)
    assert ungranted_sink.targets("secret.read") == [], (
        "an ungranted agent reached this deployment's credential store"
    )


async def test_a_newly_created_agent_starts_with_no_connections(
    deployment: _Deployment,
) -> None:
    """Adding an agent can never widen access. It has nothing until it is granted."""
    await deployment.connect()

    registry = await deployment.start_agent("brand_new")

    assert registry.tools == {} or not any(tool in registry.tools for tool in _SERVED)


# --- one account, entered once -------------------------------------------------


async def test_one_credential_serves_both_granted_agents(deployment: _Deployment) -> None:
    """The token is typed once and stored once — granting copies nothing.

    A credential copied per agent is a credential that must be rotated per agent
    and can be left behind by a revoke. The store is checked directly for the one
    coordinate, and both granted agents are then started to prove that one entry
    is what actually serves them.
    """
    await deployment.connect()

    entries = _env_entries(connector_env_file(deployment.arc_dir))
    assert list(entries) == [SecretRef(connection=_CONNECTION, field=_FIELD).env_key], (
        f"the credential is stored more than once: {sorted(entries)}"
    )

    for agent in _GRANTED:
        registry = await deployment.start_agent(agent)
        assert _SERVED[0] in registry.tools


# --- revoking ------------------------------------------------------------------


async def test_revoking_takes_the_tools_from_one_agent_and_leaves_the_other(
    deployment: _Deployment,
) -> None:
    """Revocation is enforced where tools are registered, one agent at a time."""
    await deployment.connect()
    revoked, kept = _GRANTED

    deployment.connections().revoke(_CONNECTION, [revoked])

    after_revoke = await deployment.start_agent(revoked)
    assert not any(tool in after_revoke.tools for tool in _SERVED)
    assert _SERVED[0] in (await deployment.start_agent(kept)).tools


async def test_running_agent_reconciles_grants_and_revocations_without_a_restart(
    deployment: _Deployment,
) -> None:
    """The live module replaces its owned snapshot before the next turn."""
    await deployment.connect(agents=())
    registry, capability = await deployment.start_running_agent(_GRANTED[0])
    assert not any(tool in registry.tools for tool in _SERVED)

    deployment.connections().grant(_CONNECTION, [_GRANTED[0]])
    granted = await capability.reconcile()
    assert granted.status == "applied"
    assert set(granted.tools) == set(_SERVED)
    assert set(registry.tools) >= set(_SERVED)

    repeated = await capability.reconcile()
    assert repeated.status == "applied"
    assert repeated.tools == granted.tools
    assert len([name for name in registry.tools if name in _SERVED]) == len(_SERVED)

    deployment.connections().revoke(_CONNECTION, [_GRANTED[0]])
    revoked = await capability.reconcile()
    assert revoked.status == "applied"
    assert not any(tool in registry.tools for tool in _SERVED)

    deployment.connections().grant(_CONNECTION, [_GRANTED[0]])
    assert set((await capability.reconcile()).tools) == set(_SERVED)
    await deployment.connections().remove(_CONNECTION)
    removed = await capability.reconcile()
    assert removed.status == "applied"
    assert not any(tool in registry.tools for tool in _SERVED)


async def test_live_control_applies_grant_revoke_and_remove_to_a_running_agent(
    deployment: _Deployment,
) -> None:
    """The shared management seam makes a persisted change live immediately."""
    await deployment.connect(agents=())
    registry, capability = await deployment.start_running_agent(_GRANTED[0])

    class _Control:
        async def reconcile(self, agent: str) -> Any:
            return await capability.reconcile() if agent == _GRANTED[0] else None

    connections = Connections.for_deployment(
        arc_dir=deployment.arc_dir,
        data_dir=deployment.data_dir,
        extensions_root=deployment.root,
        audit=AuditChain.held(deployment.sink),
        state_opener=deployment.open_arcstore,
        connector_control=_Control(),
    )
    granted = await connections.grant_and_reconcile(_CONNECTION, [_GRANTED[0]])
    assert granted.activations[0].status == "applied"
    assert set(granted.activations[0].tools) == set(_SERVED)
    assert _SERVED[0] in registry.tools

    revoked = await connections.revoke_and_reconcile(_CONNECTION, [_GRANTED[0]])
    assert revoked.activations[0].status == "applied"
    assert not any(tool in registry.tools for tool in _SERVED)

    await connections.grant_and_reconcile(_CONNECTION, [_GRANTED[0]])
    removed = await connections.remove_and_reconcile(_CONNECTION)
    assert removed.activations[0].status == "applied"
    assert not any(tool in registry.tools for tool in _SERVED)


async def test_durable_reconcile_commands_survive_a_management_process_restart(
    deployment: _Deployment,
) -> None:
    """A later owner consumes the persisted command and acknowledges its revision."""
    await deployment.connect(agents=())

    pending = await deployment.connections().grant_and_reconcile(_CONNECTION, [_GRANTED[0]])
    assert pending.activations[0].status == "activation_pending"
    command = (
        await deployment.arcstore_backend.mutable_query(
            "connector_reconcile_commands", where={"agent": _GRANTED[0]}
        )
    )[0]
    assert command["status"] == "pending"

    registry, capability = await deployment.start_running_agent(_GRANTED[0])
    try:
        assert set(registry.tools) >= set(_SERVED)
        ack = await deployment.arcstore_backend.mutable_read(
            "connector_reconcile_acks", command["command_id"]
        )
        assert ack is not None
        assert ack["revision"] == command["revision"]
        assert ack["status"] == "applied"

        revoked = await deployment.connections().revoke_and_reconcile(_CONNECTION, [_GRANTED[0]])
        assert revoked.activations[0].status == "activation_pending"
        await capability._drain_reconcile_commands()
        assert not any(tool in registry.tools for tool in _SERVED)

        await deployment.connections().grant_and_reconcile(_CONNECTION, [_GRANTED[0]])
        await capability._drain_reconcile_commands()
        assert set(registry.tools) >= set(_SERVED)
        removed = await deployment.connections().remove_and_reconcile(_CONNECTION)
        assert removed.activations[0].status == "activation_pending"
        await capability._drain_reconcile_commands()
        assert not any(tool in registry.tools for tool in _SERVED)
    finally:
        await capability.teardown()


async def test_durable_grant_snapshot_converges_when_enqueue_was_lost(
    deployment: _Deployment,
) -> None:
    """The owner reconciles the registry even after a mutator crashes before queueing."""
    await deployment.connect(agents=())
    deployment.connections().grant(_CONNECTION, [_GRANTED[0]])
    registry, capability = await deployment.start_running_agent(_GRANTED[0])
    try:
        assert set(registry.tools) >= set(_SERVED)

        deployment.connections().revoke(_CONNECTION, [_GRANTED[0]])
        await capability._reconcile_cycle()
        assert not any(tool in registry.tools for tool in _SERVED)
        assert (
            await deployment.arcstore_backend.mutable_query("connector_reconcile_commands") == []
        )
    finally:
        await capability.teardown()


async def test_connector_startup_does_not_require_an_arcstore_queue(
    deployment: _Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A generic agent starts from durable grants even without an ArcStore DSN."""
    monkeypatch.delenv("ARCSTORE_DATABASE_URL", raising=False)
    registry, capability = await deployment.start_running_agent(_GRANTED[0], with_arcstore=False)
    try:
        assert not any(tool in registry.tools for tool in _SERVED)
    finally:
        await capability.teardown()


async def test_one_live_control_failure_does_not_skip_later_durable_commands(
    deployment: _Deployment,
) -> None:
    """Every target is queued before a local failure can happen."""
    await deployment.connect(agents=())

    class _Control:
        async def reconcile(self, agent: str) -> Any:
            if agent == _GRANTED[0]:
                raise RuntimeError("worker restarting")
            return arcagent.ConnectorReconcileResult(status="applied", tools=_SERVED)

    import arcagent

    connections = Connections.for_deployment(
        arc_dir=deployment.arc_dir,
        data_dir=deployment.data_dir,
        extensions_root=deployment.root,
        audit=AuditChain.held(deployment.sink),
        state_opener=deployment.open_arcstore,
        connector_control=_Control(),
    )
    mutation = await connections.grant_and_reconcile(_CONNECTION, _GRANTED)

    assert [result.status for result in mutation.activations] == ["activation_pending", "applied"]
    commands = await deployment.arcstore_backend.mutable_query("connector_reconcile_commands")
    assert {row["agent"] for row in commands} == set(_GRANTED)
    assert len(await deployment.arcstore_backend.mutable_query("connector_reconcile_acks")) == 1


async def test_unstarted_connector_module_reports_activation_pending() -> None:
    pending = await Connectors().reconcile()

    assert pending.status == "activation_pending"
    assert pending.tools == ()


async def test_revoking_leaves_the_credential_for_the_agents_that_keep_it(
    deployment: _Deployment,
) -> None:
    """A revoke drops a grant, never the account. Removing the connection does that."""
    await deployment.connect()

    deployment.connections().revoke(_CONNECTION, [_GRANTED[0]])

    assert SecretRef(connection=_CONNECTION, field=_FIELD).env_key in _env_entries(
        connector_env_file(deployment.arc_dir)
    )


# --- refusals ------------------------------------------------------------------


async def test_granting_a_connection_that_does_not_exist_is_refused(
    deployment: _Deployment,
) -> None:
    """A grant against nothing must refuse, not quietly write a row nobody reads."""
    with pytest.raises(ExtensionError) as caught:
        deployment.connections().grant("no_such_account", ["coder"])

    assert caught.value.code == NO_SUCH_CONNECTION
    assert "no_such_account" in caught.value.message


async def test_granting_to_an_unusable_agent_name_is_refused(
    deployment: _Deployment,
) -> None:
    """An agent name that cannot key anything is refused where it is typed."""
    await deployment.connect()

    with pytest.raises(ExtensionError):
        deployment.connections().grant(_CONNECTION, ["Not A Name"])


# --- what the operator sees ----------------------------------------------------


async def test_the_registry_answers_who_holds_this_connection(
    deployment: _Deployment,
) -> None:
    """ "Who can read my mail" is one read of one file, for the whole deployment."""
    await deployment.connect()

    held = deployment.connections().connections()

    assert set(held) == {_CONNECTION}
    assert held[_CONNECTION].agents == _GRANTED


async def test_granting_a_third_agent_adds_it_without_disturbing_the_others(
    deployment: _Deployment,
) -> None:
    await deployment.connect()

    granted = deployment.connections().grant(_CONNECTION, [_UNGRANTED[0]])

    assert granted.agents == (*_GRANTED, _UNGRANTED[0])
    assert _SERVED[0] in (await deployment.start_agent(_UNGRANTED[0])).tools


def _env_entries(path: Path) -> dict[str, str]:
    """Read the deployment credential file the way the store writes it."""
    if not path.is_file():
        return {}
    return dict(
        line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines() if "=" in line
    )


# --- stringency: a grant may not quietly change what a connection claims -------


def _harden(deployment: _Deployment, agent: str, tier: str) -> None:
    """Give one agent a stricter tier, the way its own config declares one."""
    config = deployment.agent_dir(agent) / "arcagent.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace('tier = "personal"', f'tier = "{tier}"'),
        encoding="utf-8",
    )


def _fleet_tier(deployment: _Deployment, tier: str) -> None:
    """Set the deployment's own floor, in the file the whole stack already merges."""
    fleet = config_file("arcagent.toml", deployment.arc_dir)
    fleet.parent.mkdir(parents=True, exist_ok=True)
    fleet.write_text(f'[security]\ntier = "{tier}"\n', encoding="utf-8")


async def test_a_connection_is_served_at_its_strictest_grantee(
    deployment: _Deployment,
) -> None:
    """Two agents, one account, one store — the strictest of them sets the tier."""
    _harden(deployment, _GRANTED[1], "enterprise")

    tier = deployment.connections()._tier_for(_GRANTED)

    assert tier is Tier.ENTERPRISE


async def test_the_deployment_floor_is_a_lower_bound_not_a_default(
    deployment: _Deployment,
) -> None:
    """A per-agent downgrade must not lower a shared account below the fleet's posture."""
    _fleet_tier(deployment, "enterprise")

    assert deployment.connections()._tier_for(_GRANTED) is Tier.ENTERPRISE


async def test_granting_a_federal_agent_is_refused_rather_than_rehoming_the_credential(
    deployment: _Deployment,
) -> None:
    """The transition case. The token does not move on its own, so the grant refuses.

    Honouring it would leave a connection reporting federal stringency while its
    credential sits in this host's ``connections.env`` — a control reporting a
    posture it does not have. Re-homing the credential silently is the other half
    of that hazard, so the operator is told what to configure instead.
    """
    await deployment.connect()
    _harden(deployment, _UNGRANTED[0], "federal")

    with pytest.raises(ExtensionError) as caught:
        deployment.connections().grant(_CONNECTION, [_UNGRANTED[0]])

    assert caught.value.code == TIER_WOULD_RISE
    assert "federal" in caught.value.message


async def test_the_refused_grant_changed_nothing(deployment: _Deployment) -> None:
    """A refusal is a refusal: no half-written grant, and no new access."""
    await deployment.connect()
    _harden(deployment, _UNGRANTED[0], "federal")

    with pytest.raises(ExtensionError):
        deployment.connections().grant(_CONNECTION, [_UNGRANTED[0]])

    assert deployment.connections().connections()[_CONNECTION].agents == _GRANTED
    registry = await deployment.start_agent(_UNGRANTED[0])
    assert not any(tool in registry.tools for tool in _SERVED)


async def test_a_connector_arc_holds_no_credential_for_can_still_be_granted(
    deployment: _Deployment,
) -> None:
    """The guard is about a credential in the wrong store, not about the tier alone.

    A bundle whose own binary holds its token has nothing in any Arc store, so
    there is nothing that could be in the wrong one — refusing it would take the
    safest connectors away from exactly the deployments that need them most.
    """
    _write_cli_connection(deployment)
    _harden(deployment, _UNGRANTED[0], "federal")

    granted = deployment.connections().grant(_CLI_CONNECTION, [_UNGRANTED[0]])

    assert _UNGRANTED[0] in granted.agents


async def test_installing_for_a_stricter_agent_than_the_plan_was_taken_at_is_refused(
    deployment: _Deployment,
) -> None:
    """The plan carries the tier its manifest and egress verdict were resolved at.

    Installing it for a stricter agent would apply a personal deployment's verdicts
    to a federal agent's account, so the surface re-plans rather than keeping the
    lax answer.
    """
    _harden(deployment, _UNGRANTED[0], "federal")
    connections = deployment.connections()
    plan = connections.plan(_BUNDLE, _CONNECTION)

    with pytest.raises(ExtensionError) as caught:
        await connections.install(plan, {_FIELD: _TOKEN}, agents=[_UNGRANTED[0]])

    assert caught.value.code == PLAN_TIER_TOO_LOW


async def test_planning_for_the_agents_that_will_hold_it_takes_their_tier(
    deployment: _Deployment,
) -> None:
    """The remedy the refusal names actually works: plan for the agents, get their tier."""
    _harden(deployment, _UNGRANTED[0], "enterprise")

    plan = deployment.connections().plan(_BUNDLE, _CONNECTION, agents=[_UNGRANTED[0]])

    assert plan.tier is Tier.ENTERPRISE


#: A bundle whose binary owns its own credential — the shape half the shipped
#: connectors have, and the one Arc stores nothing for.
_CLI_CONNECTION = "hosted"


def _write_cli_connection(deployment: _Deployment) -> None:
    """A credential-less ``cli`` connection, defined directly: no install to run."""
    bundle = deployment.root / "hosted_service"
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "extension.toml").write_text(
        "[extension]\n"
        'name = "hosted_service"\n'
        'version = "1.0.0"\n'
        'attachment = "cli"\n'
        'tier_floor = "personal"\n\n'
        "[tools]\n"
        'allow = ["echo_one"]\n\n'
        "[[tools.declared]]\n"
        'name = "echo_one"\n'
        'classification = "read_only"\n\n'
        "[approval]\n"
        'default = "none"\n\n'
        "[config.cli]\n"
        'binary = "echo"\n'
        'probe_argv = ["ready"]\n\n'
        "[[config.cli.commands]]\n"
        'tool = "echo_one"\n'
        'argv = ["one"]\n'
        'description = "Echo the first message."\n'
        'classification = "read_only"\n',
        encoding="utf-8",
    )
    deployment.registry_file().define(
        _CLI_CONNECTION, Connection(extension="hosted_service", approval="none")
    )
