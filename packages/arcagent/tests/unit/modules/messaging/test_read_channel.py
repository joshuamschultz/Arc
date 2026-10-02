"""``messaging_read_channel`` — an agent catches up on a team channel (Alpha 2 item 58).

Every case drives the production tool over the real ``MessagingService``.
The read is membership-gated, no-read-up, free of other agents' un-addressed
chatter, bounded, newest-first with a cursor, and audited.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from arcteam import composition as _bootstrap
from arcteam.types import Channel, Entity, EntityType, Message
from arctrust import AgentIdentity
from packages.arcagent.tests.unit.modules.messaging.conftest import (
    make_config_dict,
    make_operator_signer,
)

from arcagent.modules.messaging import _runtime
from arcagent.modules.messaging.capabilities import messaging_read_channel

ME = "agent://reader"
HUMAN = "user://josh"


@pytest.fixture
async def st(tmp_path: Path) -> AsyncIterator[_runtime._State]:
    _runtime.reset()
    identity = AgentIdentity.generate(org="local", agent_type="agent")
    _runtime.configure(
        config=make_config_dict(entity_id=ME, entity_name="reader"),
        telemetry=MagicMock(),
        workspace=tmp_path,
        team_root=tmp_path / "team",
        agent_name="reader",
        identity=identity,
        operator_signer=make_operator_signer(),
    )
    state = _runtime.state()
    await state.registry.register(
        _bootstrap.self_entity(
            entity_id=ME,
            entity_name="Reader",
            handle="reader",
            identity=identity,
            roles=["executor"],
            capabilities=["task-execution"],
        )
    )
    human = AgentIdentity.generate(org="local", agent_type="agent")
    await state.registry.register(
        Entity(
            did=human.did,
            handle="josh",
            id=HUMAN,
            name="Josh",
            type=EntityType.USER,
            public_key=human.public_key.hex(),
        )
    )
    yield state
    _runtime.reset()


async def _channel(state: _runtime._State, name: str, members: list[str], **kw: Any) -> None:
    await state.svc.create_channel(Channel(name=name, members=members, **kw))


async def _post(
    state: _runtime._State, channel: str, body: str, sender: str = HUMAN, **kw: Any
) -> Message:
    return await state.svc.send(
        Message(sender=sender, to=[f"channel://{channel}"], body=body, **kw)
    )


async def _read(**kw: Any) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(await messaging_read_channel(**kw))
    return out


@pytest.mark.asyncio
async def test_member_reads_recent_messages_newest_first(st: _runtime._State) -> None:
    await _channel(st, "ops", [ME, HUMAN])
    for body in ("one", "two", "three"):
        await _post(st, "ops", body)
    out = await _read(channel="ops")
    assert [m["body"] for m in out["messages"]] == ["three", "two", "one"]
    assert out["channel"] == "ops"
    assert out["omitted"] == 0
    assert {"seq", "id", "sender", "body", "ts", "thread_id"} <= set(out["messages"][0])


@pytest.mark.asyncio
async def test_non_member_is_denied(st: _runtime._State) -> None:
    await _channel(st, "private", [HUMAN])
    await _post(st, "private", "secret plans")
    assert await _read(channel="private") == {"error": "not a member"}


@pytest.mark.asyncio
async def test_unknown_channel_is_denied_like_non_member(st: _runtime._State) -> None:
    assert await _read(channel="nope") == {"error": "not a member"}


@pytest.mark.asyncio
async def test_message_above_clearance_is_withheld(st: _runtime._State) -> None:
    # The send path refuses a SECRET post to a lower channel, so the laundering
    # window is a channel whose clearance was LOWERED after a SECRET post.
    await _channel(st, "ops", [ME, HUMAN], clearance="SECRET")
    await _post(st, "ops", "public")
    await _post(st, "ops", "top secret body", classification="SECRET")
    await st.svc._backend.write(
        "messages/channels",
        "ops",
        Channel(name="ops", members=[ME, HUMAN]).model_dump(),
    )
    out = await _read(channel="ops")
    assert [m["body"] for m in out["messages"]] == ["public"]
    assert out["omitted"] == 1
    assert "top secret body" not in json.dumps(out)


@pytest.mark.asyncio
async def test_channel_above_clearance_is_denied(st: _runtime._State) -> None:
    await _channel(st, "vault", [ME], clearance="SECRET")
    assert await _read(channel="vault") == {"error": "not a member"}


@pytest.mark.asyncio
async def test_other_agents_unaddressed_posts_are_excluded(st: _runtime._State) -> None:
    peer_identity = AgentIdentity.generate(org="local", agent_type="agent")
    peer = Entity(
        did=peer_identity.did,
        handle="brad",
        id="agent://brad",
        name="Brad",
        type=EntityType.AGENT,
        public_key=peer_identity.public_key.hex(),
    )
    await st.registry.register(peer)
    await _channel(st, "ops", [ME, HUMAN, peer.id])
    await _post(st, "ops", "human says hi")
    operator_signer = st.svc._signer
    st.svc._signer = SimpleNamespace(
        did=peer_identity.did, private_key=peer_identity.signing_seed
    )  # a peer's posts carry the peer's own signature
    try:
        await _post(st, "ops", "brad chatter", sender=peer.id)
        await _post(st, "ops", "@reader brad to me", sender=peer.id)
    finally:
        st.svc._signer = operator_signer
    out = await _read(channel="ops")
    bodies = [m["body"] for m in out["messages"]]
    assert "brad chatter" not in bodies
    assert "@reader brad to me" in bodies
    assert "human says hi" in bodies
    assert out["omitted"] == 1


@pytest.mark.asyncio
async def test_page_is_bounded_and_cursor_pages_back(st: _runtime._State) -> None:
    await _channel(st, "ops", [ME, HUMAN])
    for i in range(120):
        await _post(st, "ops", f"m{i}")
    out = await _read(channel="ops", limit=10_000)
    assert len(out["messages"]) == 50
    assert out["messages"][0]["body"] == "m119"
    older = await _read(channel="ops", limit=10, before_seq=out["next_before_seq"])
    assert older["messages"][0]["body"] == "m69"
    assert len(older["messages"]) == 10
    assert (await _read(channel="ops", limit=-5))["messages"] != []


@pytest.mark.asyncio
async def test_byte_cap_truncates_page(st: _runtime._State) -> None:
    await _channel(st, "ops", [ME, HUMAN])
    for _ in range(30):
        await _post(st, "ops", "x" * 3900)
    out = await _read(channel="ops", limit=50)
    assert sum(len(m["body"]) for m in out["messages"]) <= 16_000
    assert out["truncated"] is True
    assert out["next_before_seq"] is not None


@pytest.mark.asyncio
async def test_read_is_audited(st: _runtime._State) -> None:
    await _channel(st, "ops", [ME, HUMAN])
    await _post(st, "ops", "hello")
    await _read(channel="ops")
    await _read(channel="elsewhere")
    events = [c.args for c in st.telemetry.audit_event.call_args_list]
    reads = [a for a in events if a[0] == "messaging.channel_read"]
    assert reads[0][1]["channel"] == "ops"
    assert reads[0][1]["returned"] == 1
    assert reads[1][1]["outcome"] == "denied"


def test_channel_wake_prompt_names_the_tool() -> None:
    from arcagent.modules.messaging.capabilities import _format_delivery

    msg = Message(sender=HUMAN, to=["channel://ops"], body="status?")
    assert "messaging_read_channel" in _format_delivery(msg)


@pytest.mark.asyncio
async def test_mail_wake_prompt_names_the_tool(st: _runtime._State) -> None:
    from arcagent.modules.messaging import mail_turn

    msg = Message(sender=HUMAN, to=[ME], body="hi", subject="s")
    assert "messaging_read_channel" in await mail_turn.format_delivery(st, msg)


@pytest.mark.asyncio
async def test_member_of_one_channel_cannot_read_another(st: _runtime._State) -> None:
    await _channel(st, "mine", [ME, HUMAN])
    await _channel(st, "theirs", [HUMAN])
    await _post(st, "mine", "ok")
    await _post(st, "theirs", "not yours")
    for alias in ("theirs", "channel://theirs", "mine/../theirs", "THEIRS", "*"):
        out = await _read(channel=alias)
        assert out == {"error": "not a member"}, alias
        assert "not yours" not in json.dumps(out)
