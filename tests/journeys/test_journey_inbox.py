# ruff: noqa: F811  # fixtures are imported from the e2e and re-bound as test parameters
"""Journey: the operator mails an agent from arcui, and the conversation obeys the one-reply rule.

A real ``nats-server`` carries the signed mail. Real: arcui's compose and reply
routes behind the bearer-token middleware, arcteam's signed ``MessagingService``,
the one-reply rule, the durable inbox projection and its outbox, and the agent's
messaging module (admission, wake prompt, ``agent:post_respond`` finalizer).
Faked: only the model — each agent's turn ends in a scripted final text.

What a user would see:

1. The operator composes in arcui; the agent is woken, and its answer is the one
   reply in the *same* thread.
2. A second reply into that thread is refused, and the refusal names the channel.
3. Two agents: the answer to an agent's question wakes the asker, whose own reply
   is refused — agent-to-agent mail stops after one reply.
4. An FYI (the agent has nothing to say) gets no reply at all.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import httpx
import pytest
from arcgateway.team_roster import RosterEntry
from arcstore.inbox import ParticipantRole
from arcstore.inbox_projection import participant, thread_id_for
from arcteam.mail import MAIL_THREAD_CLOSED, MailSendRequest
from arcteam.types import EntityType
from arctrust import AgentIdentity
from arcui.audit import UIAuditLogger
from arcui.auth import AuthConfig, AuthMiddleware
from arcui.routes.agent_detail import routes
from starlette.applications import Starlette

from tests.integration.test_mail_turn_e2e import (  # noqa: F401  # fixtures + helpers shared with the e2e
    _eventually,
    _Fleet,
    _has,
    _human,
    _ScriptedAgent,
    _start,
    _stop,
    backend,
    fleet,
    server_url,
)

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(shutil.which("nats-server") is None, reason="nats-server not installed"),
]


def _roster_entry(agent: _ScriptedAgent) -> RosterEntry:
    return RosterEntry(
        agent_id=agent.handle,
        name=agent.handle,
        did=agent.identity.did,
        org=None,
        type=None,
        workspace_path=str(Path("/nonexistent")),
        model=None,
        provider=None,
        online=True,
        display_name=agent.handle,
        color="#000000",
        role_label="",
        hidden=False,
    )


def _arcui(
    fleet: _Fleet, operator: AgentIdentity, agents: list[_ScriptedAgent]
) -> tuple[httpx.AsyncClient, dict[str, str]]:
    """The real arcui inbox routes, wired to the fleet's real mail service."""
    auth = AuthConfig({"viewer_token": "viewer-tok", "operator_token": "operator-tok"})
    app = Starlette(routes=routes)
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.audit = UIAuditLogger(enabled=False)
    app.state.inbox_service = fleet.inbox
    app.state.agent_mail = fleet.mail(operator)
    app.state.inbox_clearance = "UNCLASSIFIED"
    app.state.roster_provider = lambda: [_roster_entry(a) for a in agents]
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://arcui")
    return client, {"Authorization": f"Bearer {auth.operator_token}"}


async def _compose(
    client: httpx.AsyncClient, headers: dict[str, str], agent: str, subject: str, body: str
) -> str:
    response = await client.post(
        f"/api/agents/{agent}/inbox",
        json={"subject": subject, "body": body},
        headers={**headers, "Idempotency-Key": f"compose-{subject}"},
    )
    assert response.status_code == 201, response.text
    conversation: str = response.json()["conversation_id"]
    return conversation


async def _thread_bodies(
    client: httpx.AsyncClient, headers: dict[str, str], agent: _ScriptedAgent, conversation: str
) -> list[str]:
    response = await client.get(
        f"/api/agents/{agent.handle}/inbox/{thread_id_for(agent.identity.did, conversation)}",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return [m["body"] for m in response.json()["messages"]]


async def test_compose_in_arcui_is_answered_once_then_refused_with_the_channel_hint(
    fleet: _Fleet, tmp_path: Path
) -> None:
    operator = AgentIdentity.generate("arc", "operator")
    beta = _ScriptedAgent(
        "beta", AgentIdentity.generate("arc", "executor"), ["Q3 revenue was 4.2M.", ""]
    )
    await fleet.register(operator, "operator", EntityType.USER)
    await fleet.register(beta.identity, "beta", EntityType.AGENT)
    await _start(beta, fleet, tmp_path / "beta")
    client, headers = _arcui(fleet, operator, [beta])
    try:
        conversation = await _compose(
            client, headers, "beta", "Q3 numbers", "What was Q3 revenue?"
        )

        await asyncio.wait_for(beta.turns.get(), timeout=10)

        async def answered() -> bool:
            return await _has(fleet, _human(operator), conversation, 2)

        await _eventually(answered)
        assert await _thread_bodies(client, headers, beta, conversation) == [
            "What was Q3 revenue?",
            "Q3 revenue was 4.2M.",
        ]
        assert "@operator" in beta.prompts[0]
        assert conversation in beta.prompts[0]

        # A second reply into the answered thread is refused, naming the channel.
        refused = await client.post(
            f"/api/agents/beta/inbox/{thread_id_for(beta.identity.did, conversation)}/reply",
            json={"body": "And Q4?"},
            headers={**headers, "Idempotency-Key": "follow-up"},
        )
        assert refused.status_code == 409
        assert refused.json()["error"] == "mail_thread_closed"
        assert refused.json()["detail"] == MAIL_THREAD_CLOSED
        assert "channel://" in refused.json()["detail"]
        assert len(await _thread_bodies(client, headers, beta, conversation)) == 2
    finally:
        await client.aclose()
        await _stop(beta)


async def test_fyi_composed_in_arcui_gets_no_reply(fleet: _Fleet, tmp_path: Path) -> None:
    operator = AgentIdentity.generate("arc", "operator")
    beta = _ScriptedAgent("beta", AgentIdentity.generate("arc", "executor"), [""])
    await fleet.register(operator, "operator", EntityType.USER)
    await fleet.register(beta.identity, "beta", EntityType.AGENT)
    await _start(beta, fleet, tmp_path / "beta")
    client, headers = _arcui(fleet, operator, [beta])
    try:
        conversation = await _compose(
            client, headers, "beta", "Deploy done", "FYI: the deploy finished."
        )

        assert await asyncio.wait_for(beta.turns.get(), timeout=10) == ""
        await asyncio.sleep(0.4)

        assert await _thread_bodies(client, headers, beta, conversation) == [
            "FYI: the deploy finished."
        ]
    finally:
        await client.aclose()
        await _stop(beta)


async def test_agent_to_agent_mail_stops_after_one_reply(fleet: _Fleet, tmp_path: Path) -> None:
    alpha = _ScriptedAgent("alpha", AgentIdentity.generate("arc", "executor"), ["Thanks, and Q4?"])
    beta = _ScriptedAgent("beta", AgentIdentity.generate("arc", "executor"), ["Q3 was 4.2M."])
    await fleet.register(alpha.identity, "alpha", EntityType.AGENT)
    await fleet.register(beta.identity, "beta", EntityType.AGENT)
    await _start(alpha, fleet, tmp_path / "alpha")
    await _start(beta, fleet, tmp_path / "beta")
    try:
        sent = await fleet.mail(alpha.identity).send(
            MailSendRequest(
                sender="agent://alpha",
                sender_did=alpha.identity.did,
                to=("agent://beta",),
                subject="Q3",
                body="What was Q3 revenue?",
                idempotency_key="ask-q3",
            )
        )

        assert await asyncio.wait_for(beta.turns.get(), timeout=10) == "Q3 was 4.2M."
        # beta's answer wakes alpha; alpha's reply would be a reply to a reply.
        assert await asyncio.wait_for(alpha.turns.get(), timeout=10) == "Thanks, and Q4?"
        await asyncio.sleep(0.5)

        page = await fleet.inbox.list_messages(
            thread_id_for(alpha.identity.did, sent.thread_id),
            reader=participant(alpha.identity.did, role=ParticipantRole.AGENT),
        )
        assert [m.body for m in page.items] == ["What was Q3 revenue?", "Q3 was 4.2M."]
        assert beta.turns.empty()
    finally:
        await _stop(alpha, beta)
