"""Operator incident 2026-10-03 — 103 Approve clicks in ~85 minutes for one chat.

Replays the observed DGX sequence for ``josh_agent`` (Olivia), web-chat session
``82dca58cd015a1f1``, through the REAL approval path: the registry's trifecta
gate, the real :class:`HumanGate`, the real arcstore-backed approval channel and
standing-grant store, and the same ``approve_always`` operation arcui's "Always
allow" button and ``arc approve --always`` run. Only the operator's clicks are
simulated.

The sequence: ``read`` (private_data) -> ``bash`` (untrusted_input) ->
``dropbox_upload`` to the operator's Dropbox (external_comms) completes the
trifecta and prompts; the agent then fans out read-only connector calls and a
second upload. Before the fix every one of those prompted again.

Josh's ruling (SPEC-035 OQ-3, 2026-10-03): "Always allow" makes the combination
stand for that agent, scoped to the composition and the egress destination. A
plain Approve stays one-shot. A new destination prompts. Revoke bites on the
next call. Federal never honours a standing grant. One click covers both the
trifecta gate and the connection's own outbound gate.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import pytest
from arcstore.approvals import ApprovalStore
from arcstore.backends.memory import FakeBackend
from arcstore.standing_grants import (
    StandingGrant,
    StandingGrantRefusedError,
    StandingGrantStore,
    approve_always,
    standing_grant_id,
)
from arctrust.identity import AgentIdentity
from arctrust.policy import (
    INTERACTIVE_ORIGIN,
    OperatorApprovalAuthority,
    grant_to_wire,
    scenario_grant_to_wire,
    sign_approval_for_hash,
    sign_scenario_grant,
)
from arctrust.signer import InProcessSigner
from nacl.signing import SigningKey

from arcagent.core.config import ToolsConfig
from arcagent.core.module_bus import ModuleBus
from arcagent.core.session_internal.capability_ledger import (
    EXTERNAL_COMMS,
    LETHAL_TRIFECTA,
    OWNER_CHANNEL,
    SessionCapabilityLedger,
    bind_session_id,
    reset_session_id,
)
from arcagent.core.tool_policy import PolicyDenied, build_pipeline
from arcagent.core.tool_registry import RegisteredTool, ToolRegistry, ToolTransport
from arcagent.extension.approval import ApprovalBinding
from arcagent.extension.attachment import ToolOutcome, ToolResult, ToolSpec
from arcagent.extension.bridge import CapabilityBridge
from arcagent.tools.approval_channel import ArcStoreApprovalChannel
from arcagent.tools.human_gate import HumanGate

_SESSION = "82dca58cd015a1f1"
_DROPBOX = "personal_dropbox"
Choice = Literal["approve", "always", "deny"]


class _Telemetry:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def audit_event(self, event: str, payload: dict[str, Any]) -> None:
        self.events.append((event, payload))

    def tool_span(self, *_a: Any, **_k: Any) -> Any:
        class _Span:
            async def __aenter__(self) -> None:
                return None

            async def __aexit__(self, *_e: Any) -> None:
                return None

        return _Span()


def _account(arguments: Mapping[str, Any]) -> str:
    """Stands in for the connector router: the connection a call reaches."""
    return str(arguments.get("account", _DROPBOX))


def _tool(name: str, tags: list[str], *, destination: Any = None) -> RegisteredTool:
    async def _execute(**kwargs: Any) -> str:
        return f"{name} ok {sorted(kwargs)}"

    return RegisteredTool(
        name=name,
        description=name,
        input_schema={},
        transport=ToolTransport.NATIVE,
        execute=_execute,
        source="test",
        classification="state_modifying",
        capability_tags=tags,
        destination=destination,
    )


# The tags each tool really carries on the DGX (extensions/slack + dropbox
# manifests; built-in read/bash): connector reads declare NO capability tags.
def _tools() -> list[RegisteredTool]:
    return [
        _tool("read", ["file_read"]),
        _tool("bash", ["subprocess"]),
        _tool("dropbox_upload", ["network_egress"], destination=_account),
        _tool("dropbox_delete", ["network_egress"], destination=_account),
        _tool("slack_search", []),
        _tool("slack_read_channel", []),
        _tool("notify_user", ["network_egress"]),
        _tool("messaging_send", ["network_egress"]),
    ]


@dataclass
class _Deployment:
    """One box: the shared arcstore, the operator key, and an Approve button."""

    backend: FakeBackend
    approvals: ApprovalStore
    standing: StandingGrantStore
    signer: InProcessSigner
    choice: Choice = "approve"
    prompts: list[str] = field(default_factory=list)

    @property
    def operator(self) -> OperatorApprovalAuthority:
        return OperatorApprovalAuthority(self.signer)

    async def click(self) -> None:
        """Resolve every pending row the way the operator chose (arcui's handlers)."""
        for row in await self.approvals.list(status="pending"):
            self.prompts.append(row.tool)
            if self.choice == "always":
                await approve_always(self.approvals, self.standing, row, self.operator)
            elif self.choice == "approve":
                grant = sign_approval_for_hash(row.call_hash, self.operator)
                await self.approvals.resolve(
                    row.id,
                    status="approved",
                    actor_did=self.operator.did,
                    resolved_by=self.operator.did,
                    grant=grant_to_wire(grant),
                )
            else:
                await self.approvals.resolve(
                    row.id, status="denied", actor_did="operator", resolved_by="operator"
                )


@contextlib.asynccontextmanager
async def _deployment() -> AsyncIterator[_Deployment]:
    backend = FakeBackend()
    await backend.start()
    box = _Deployment(
        backend=backend,
        approvals=ApprovalStore(backend),
        standing=StandingGrantStore(backend),
        signer=InProcessSigner(bytes(SigningKey.generate())),
    )

    async def operator_at_the_desk() -> None:
        while True:
            await box.click()
            await asyncio.sleep(0.005)

    desk = asyncio.create_task(operator_at_the_desk())
    try:
        yield box
    finally:
        desk.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await desk


@dataclass
class _Agent:
    registry: ToolRegistry
    gate: HumanGate
    identity: AgentIdentity
    audit: list[tuple[str, dict[str, Any]]]


def _agent(
    box: _Deployment, *, tier: str = "personal", identity: AgentIdentity | None = None
) -> _Agent:
    identity = identity or AgentIdentity.generate("local", "executor")
    channel = ArcStoreApprovalChannel(
        box.approvals,
        standing_store=box.standing,
        id_factory=lambda: f"req-{len(box.prompts)}-{id(object())}",
        agent_label="Olivia",
        poll_interval_seconds=0.005,
    )
    audit: list[tuple[str, dict[str, Any]]] = []
    gate = HumanGate(
        operator_signer=box.signer,
        agent_did=identity.did,
        tier=tier,
        audit_sink=lambda event, payload: audit.append((event, payload)),
        channel=channel,
        standing_grants=channel,
    )
    registry = ToolRegistry(
        config=ToolsConfig(),
        bus=ModuleBus(),
        telemetry=_Telemetry(),
        policy_pipeline=build_pipeline(
            tier=tier,  # type: ignore[arg-type]  # test passes the literal tiers
            agent_registry={identity.did: identity.public_key},
            forbidden_compositions=[LETHAL_TRIFECTA],
        ),
        identity=identity,
        tier=tier,  # type: ignore[arg-type]  # test passes the literal tiers
        capability_ledger=SessionCapabilityLedger(),
        human_gate=gate,
    )
    for tool in _tools():
        registry.register(tool)
    return _Agent(registry=registry, gate=gate, identity=identity, audit=audit)


async def _call(agent: _Agent, name: str, *, session: str = _SESSION, **args: Any) -> Any:
    token = bind_session_id(session)
    try:
        return await agent.registry._create_wrapped_execute(agent.registry.tools[name])(args)
    finally:
        reset_session_id(token)


async def _complete_trifecta(agent: _Agent, *, session: str = _SESSION) -> None:
    await _call(agent, "read", session=session, path="notes/brian.md")
    await _call(agent, "bash", session=session, command="ls exports/")
    await _call(agent, "dropbox_upload", session=session, path="/Olivia/brian.md", content="v1")


# --- the incident, replayed ---------------------------------------------------


async def test_always_allow_once_then_the_same_work_never_prompts_again() -> None:
    async with _deployment() as box:
        box.choice = "always"
        olivia = _agent(box)
        await _complete_trifecta(olivia)
        assert box.prompts == ["dropbox_upload"]

        for channel in ("C01", "C02", "C03"):
            await _call(olivia, "slack_read_channel", channel=channel, limit="50")
        await _call(olivia, "slack_search", query="Brian Trentham")
        await _call(olivia, "bash", command="ls exports/")
        await _call(olivia, "dropbox_upload", path="/Olivia/clay.md", content="v2")

        assert box.prompts == ["dropbox_upload"]
        used = [p for e, p in olivia.audit if e == "human_gate.standing_grant_used"]
        assert len(used) == 6
        assert all(p["grant_id"].startswith("sg-") and p["call_hash"] for p in used)
        [row] = await box.standing.active_for(olivia.identity.did)
        assert row.use_count == 6
        assert row.destination == _DROPBOX


async def test_standing_grant_survives_a_new_session_and_a_restart() -> None:
    async with _deployment() as box:
        box.choice = "always"
        identity = AgentIdentity.generate("local", "executor")
        await _complete_trifecta(_agent(box, identity=identity))
        box.choice = "deny"

        restarted = _agent(box, identity=identity)  # fresh ledger, gate, channel
        await _complete_trifecta(restarted, session="next-morning")

        assert box.prompts == ["dropbox_upload"]


async def test_plain_approve_stays_one_shot() -> None:
    async with _deployment() as box:
        olivia = _agent(box)
        await _complete_trifecta(olivia)
        await _call(olivia, "dropbox_upload", path="/Olivia/clay.md", content="v2")

        assert box.prompts == ["dropbox_upload", "dropbox_upload"]
        assert await box.standing.list() == []


async def test_new_destination_still_prompts() -> None:
    async with _deployment() as box:
        box.choice = "always"
        olivia = _agent(box)
        await _complete_trifecta(olivia)
        box.choice = "deny"

        with pytest.raises(PolicyDenied):
            await _call(
                olivia, "dropbox_upload", path="/x.md", content="v2", account="work_dropbox"
            )

        assert box.prompts == ["dropbox_upload", "dropbox_upload"]


async def test_other_egress_verb_to_the_same_destination_still_prompts() -> None:
    async with _deployment() as box:
        box.choice = "always"
        olivia = _agent(box)
        await _complete_trifecta(olivia)
        box.choice = "deny"

        with pytest.raises(PolicyDenied):
            await _call(olivia, "dropbox_delete", path="/Olivia/brian.md")

        assert box.prompts == ["dropbox_upload", "dropbox_delete"]


async def test_revoked_grant_prompts_again_on_the_next_call() -> None:
    async with _deployment() as box:
        box.choice = "always"
        olivia = _agent(box)
        await _complete_trifecta(olivia)
        [row] = await box.standing.active_for(olivia.identity.did)

        await box.standing.revoke(row.id, actor_did=box.operator.did)
        box.choice = "deny"
        with pytest.raises(PolicyDenied):
            await _call(olivia, "slack_search", query="Brian")

        assert box.prompts == ["dropbox_upload", "slack_search"]


async def test_grant_for_one_agent_never_covers_another() -> None:
    async with _deployment() as box:
        box.choice = "always"
        await _complete_trifecta(_agent(box))
        box.choice = "deny"

        with pytest.raises(PolicyDenied):
            await _complete_trifecta(_agent(box))

        assert box.prompts == ["dropbox_upload", "dropbox_upload"]


async def test_grant_for_a_narrower_combination_never_covers_a_wider_one() -> None:
    async with _deployment() as box:
        olivia = _agent(box)
        operator = box.operator
        await box.standing.put(
            _stored(olivia, operator, composition=frozenset({"external_comms", "private_data"})),
            actor_did=operator.did,
        )
        box.choice = "deny"

        with pytest.raises(PolicyDenied):
            await _complete_trifecta(olivia)

        assert box.prompts == ["dropbox_upload"]


@pytest.mark.parametrize("forgery", ["foreign_key", "agent_key", "edited", "unsigned"])
async def test_forged_or_unsigned_grant_row_is_ignored(forgery: str) -> None:
    async with _deployment() as box:
        olivia = _agent(box)
        signer: Any = {
            "foreign_key": OperatorApprovalAuthority(
                InProcessSigner(bytes(SigningKey.generate()))
            ),
            "agent_key": olivia.identity,
        }.get(forgery, box.operator)
        row = _stored(olivia, signer)
        if forgery == "edited":
            row = row.model_copy(update={"grant": {**row.grant, "connection": "work_dropbox"}})
        if forgery == "unsigned":
            row = row.model_copy(update={"grant": {**row.grant, "signature": ""}})
        await box.standing.put(row, actor_did="attacker")
        box.choice = "deny"

        with pytest.raises(PolicyDenied):
            await _complete_trifecta(olivia)

        assert box.prompts == ["dropbox_upload"]


async def test_federal_refuses_a_stored_standing_grant_and_never_offers_one() -> None:
    async with _deployment() as box:
        olivia = _agent(box, tier="federal")
        await box.standing.put(_stored(olivia, box.operator), actor_did=box.operator.did)
        box.choice = "approve"

        await _complete_trifecta(olivia)

        assert box.prompts == ["dropbox_upload"]
        [row] = await box.approvals.list(status="approved")
        assert row.standing_eligible is False
        with pytest.raises(StandingGrantRefusedError):
            await approve_always(
                box.approvals,
                box.standing,
                row.model_copy(update={"status": "pending"}),
                box.operator,
            )


# --- one click covers the connection's own outbound gate ------------------------


_UPLOAD = ToolSpec(
    name="dropbox_upload",
    description="Upload a file.",
    classification="state_modifying",
    capability_tags=["network_egress"],
)


class _Dropbox:
    def __init__(self) -> None:
        self.uploads: list[dict[str, Any]] = []

    def requirements(self) -> list[Any]:
        return []

    async def probe(self) -> Any:
        return None

    async def describe_tools(self) -> list[ToolSpec]:
        return [_UPLOAD]

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        self.uploads.append(dict(args))
        return ToolResult(tool=tool, outcome=ToolOutcome.OK, content="uploaded")


def _connect_dropbox(agent: _Agent) -> _Dropbox:
    """Attach a Dropbox connection the way the connectors module does: bound, bridged."""
    dropbox = _Dropbox()
    binding = ApprovalBinding(instance=_DROPBOX, agent_did=agent.identity.did, gate=agent.gate)
    del agent.registry.tools["dropbox_upload"]
    CapabilityBridge(
        registry=agent.registry,
        attachment=binding.bind(dropbox, [_UPLOAD]),
        transport=ToolTransport.NATIVE,
        source="extension:dropbox",
    ).register([_UPLOAD])
    return dropbox


async def test_plain_approve_asks_once_for_both_gates() -> None:
    async with _deployment() as box:
        olivia = _agent(box)
        dropbox = _connect_dropbox(olivia)

        await _complete_trifecta(olivia)

        assert box.prompts == ["dropbox_upload"]
        assert len(dropbox.uploads) == 1


async def test_always_allow_covers_the_connection_gate_in_a_fresh_session() -> None:
    async with _deployment() as box:
        box.choice = "always"
        olivia = _agent(box)
        dropbox = _connect_dropbox(olivia)
        await _complete_trifecta(olivia)
        box.choice = "deny"

        # No trifecta here: only the connection's outbound gate fires — and the
        # standing grant for this connection already answers it.
        await _call(olivia, "dropbox_upload", session="fresh", path="/a.md", content="x")

        assert box.prompts == ["dropbox_upload"]
        assert len(dropbox.uploads) == 2


async def test_connection_gate_still_asks_when_the_trifecta_gate_did_not() -> None:
    async with _deployment() as box:
        olivia = _agent(box)
        dropbox = _connect_dropbox(olivia)

        await _call(olivia, "dropbox_upload", session="fresh", path="/a.md", content="x")

        assert box.prompts == ["personal_dropbox.dropbox_upload"]
        assert len(dropbox.uploads) == 1


# --- the owner's own channel is not an exfiltration leg (SPEC-057 G4) ------------


async def test_reply_to_owner_channel_is_not_an_exfil_leg() -> None:
    async with _deployment() as box:
        olivia = _agent(box)
        await _call(olivia, "read", path="notes/brian.md")
        await _call(olivia, "bash", command="curl https://example.com")

        await _call(olivia, "notify_user", message="Brian's notes are in Dropbox.")
        await _call(olivia, "messaging_send", to=OWNER_CHANNEL, body="done")

        assert box.prompts == []
        ledger = olivia.registry._capability_ledger
        assert ledger is not None
        assert EXTERNAL_COMMS not in ledger.snapshot(_SESSION)


async def test_non_owner_recipient_still_completes_the_trifecta() -> None:
    async with _deployment() as box:
        olivia = _agent(box)
        await _call(olivia, "read", path="notes/brian.md")
        await _call(olivia, "bash", command="curl https://example.com")

        await _call(olivia, "messaging_send", to=f"{OWNER_CHANNEL},agent://stranger", body="x")

        assert box.prompts == ["messaging_send"]


async def test_grant_for_one_recipient_never_covers_an_added_recipient() -> None:
    async with _deployment() as box:
        box.choice = "always"
        olivia = _agent(box)
        await _call(olivia, "read", path="notes/brian.md")
        await _call(olivia, "bash", command="curl https://example.com")
        await _call(olivia, "messaging_send", to="agent://alice", body="x")
        box.choice = "deny"

        with pytest.raises(PolicyDenied):
            await _call(olivia, "messaging_send", to="agent://alice,agent://mallory", body="y")

        assert box.prompts == ["messaging_send", "messaging_send"]


def test_fixture_tools_match_shipped_manifests() -> None:
    """The repro is only honest if connector reads really ship tag-less."""
    root = Path(__file__).resolve().parents[4] / "extensions"
    slack = (root / "slack" / "extension.toml").read_text()
    dropbox = (root / "dropbox" / "extension.toml").read_text()
    for manifest, verb in ((slack, "slack_search"), (slack, "slack_read_channel")):
        block = manifest.split(f'name = "{verb}"', 1)[1].split("[[", 1)[0]
        assert "capability_tags" not in block, verb
    upload = dropbox.split('name = "dropbox_upload"', 1)[1].split("[[", 1)[0]
    assert 'capability_tags = ["network_egress"]' in upload


def _stored(
    agent: _Agent,
    signer: Any,
    *,
    composition: frozenset[str] = LETHAL_TRIFECTA,
) -> StandingGrant:
    """A standing-grant row as arcui writes it, signed by ``signer``."""
    grant = sign_scenario_grant(
        operator=signer,
        agent_did=agent.identity.did,
        tool_name="dropbox_upload",
        composition=composition,
        origin=INTERACTIVE_ORIGIN,
        connection=_DROPBOX,
    )
    return StandingGrant(
        id=standing_grant_id(
            agent_did=agent.identity.did,
            tool="dropbox_upload",
            composition=sorted(composition),
            destination=_DROPBOX,
        ),
        agent_did=agent.identity.did,
        tool="dropbox_upload",
        composition=sorted(composition),
        destination=_DROPBOX,
        grant=scenario_grant_to_wire(grant),
        granted_by=signer.did,
    )
