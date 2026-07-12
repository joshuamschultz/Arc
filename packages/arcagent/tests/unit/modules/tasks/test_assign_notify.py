"""SPEC-056 Phase C — cross-agent assignment notification (TDD RED, arcagent side).

SDD §5 splits assignment into two concerns: arcstore (durable owner write) and
arcteam (the signed hand-off envelope). This file assumes three seams that do
not exist yet:

1. ``_runtime._State`` grows a ``messenger`` field (mirrors the existing
   ``registry``/``store`` fields — injectable for tests, built lazily
   otherwise) and ``_runtime.configure()`` accepts a ``messenger=`` kwarg.
2. ``capabilities.assign_task`` — AFTER the arcstore ``st.store.assign(...)``
   write succeeds — sends exactly ONE ``MsgType.TASK_ASSIGNED`` message to the
   assignee's inbox (``to=["agent://<handle>"]``), body carrying the `@handle`
   mention (mentions.py:44 overwrites any `mentions=` set directly, so the
   handle MUST be textual in the body) plus the task id and a terse summary.
   A notify failure must never roll back or mask the already-successful
   arcstore write — ``assign_task`` still returns the updated task.
3. ``arcagent.modules.tasks.handlers.handle_task_assigned(message)`` — the
   assignee-side adopt handler — parses the task id out of the delivered
   envelope and calls ``start_task`` for it (SDD §5: "the assignee's loop, on
   the delivery, calls claim_task/start_task for that id").

None of ``arcagent.modules.tasks.handlers`` exists yet, and neither
``_runtime``'s ``messenger`` seam nor ``assign_task``'s notify call does —
every test here fails (ImportError / TypeError / assertion) until Phase C
lands. Imports are local to each test (mirrors ``test_capabilities.py``) so a
missing module surfaces as one failure per test, not a collection error
masking the rest.

KNOWN CROSS-PHASE GAP (flagged for whoever implements Phase C, not fixed
here — out of scope for an arcteam/arcagent-only RED pass): arcstore's
``TaskStore.start_task`` (tasks.py:191-213) requires ``where={"owner_did":
None}`` to win the atomic claim. ``assign()`` (tasks.py:215-233) only patches
``owner_did`` — it never sets ``status="in_progress"`` — so immediately after
an assignment the task is owned but NOT in_progress, and the assignee's very
first ``start_task`` call will lose the atomic claim (owner_did is no longer
NULL) and return ``no_tasks_available`` rather than adopting the task. SDD §5
explicitly assumes ``start_task`` "just works" for this case. Making
``TestAdoptHandlerStartsAssignedTask`` below pass end-to-end will also
require loosening ``start_task``'s ``where`` clause to accept
``owner_did IN (NULL, agent_did)`` (a Phase A follow-on), not just adding the
Phase C handler.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from arctrust import AgentIdentity

from tests.unit.modules.tasks.conftest import make_peer_entity, make_registry


class _FakeMessenger:
    """Records every ``send`` call; ``fail=True`` simulates a delivery outage."""

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[Any] = []
        self._fail = fail

    async def send(self, message: Any) -> Any:
        if self._fail:
            msg = "messenger unavailable"
            raise RuntimeError(msg)
        self.sent.append(message)
        return message


@pytest.fixture
def notify_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Bootstrap the runtime with an injected fake messenger + a registered peer.

    Mirrors ``tasks_state``/``peer_state`` from ``test_capabilities.py`` but
    additionally threads a ``messenger=`` kwarg through ``_runtime.configure``
    — the assumed injection seam for Phase C (point 1 in the module docstring).
    """
    from arcagent.modules.tasks import _runtime

    monkeypatch.delenv("ARCSTORE_DATA_DIR", raising=False)
    _runtime.reset()
    identity = AgentIdentity.generate(org="local", agent_type="agent")
    registry = make_registry()
    fake = _FakeMessenger()
    _runtime.configure(
        config={"enabled": True, "data_dir": str(tmp_path)},
        telemetry=MagicMock(),
        workspace=tmp_path,
        identity=identity,
        registry=registry,
        messenger=fake,
    )
    st = _runtime.state()
    bob_identity = AgentIdentity.generate(org="local", agent_type="agent")
    peer = make_peer_entity("bob", "Bob").model_copy(update={"did": bob_identity.did})
    yield st, fake, bob_identity, peer
    _runtime.reset()


@pytest.mark.asyncio
class TestAssignTaskNotifiesAssignee:
    async def test_assign_sends_exactly_one_task_assigned_message(
        self, notify_state: Any
    ) -> None:
        from arcagent.modules.tasks.capabilities import assign_task, create_task

        st, fake, _bob_identity, bob_entity = notify_state
        await st.registry.register(bob_entity)

        created = json.loads(await create_task(title="Ship the release", owner=""))
        result = json.loads(await assign_task(id=created["id"], to_handle="@bob"))

        assert result["owner_did"] == bob_entity.did
        assert len(fake.sent) == 1

    async def test_notification_addresses_assignee_inbox_with_correct_type(
        self, notify_state: Any
    ) -> None:
        from arcteam.types import MsgType

        from arcagent.modules.tasks.capabilities import assign_task, create_task

        st, fake, _bob_identity, bob_entity = notify_state
        await st.registry.register(bob_entity)

        created = json.loads(await create_task(title="Ship the release", owner=""))
        await assign_task(id=created["id"], to_handle="@bob")

        sent = fake.sent[0]
        assert sent.to == ["agent://bob"]
        assert sent.msg_type == MsgType.TASK_ASSIGNED

    async def test_notification_body_carries_the_handle_mention_and_task_id(
        self, notify_state: Any
    ) -> None:
        # mentions.py:44 OVERWRITES any `mentions=` set directly on the
        # message — the assignee handle must be literal `@bob` text in the
        # body, or the notification never raises the assignee's attention
        # flags / never resolves to a routable mention.
        from arcagent.modules.tasks.capabilities import assign_task, create_task

        st, fake, _bob_identity, bob_entity = notify_state
        await st.registry.register(bob_entity)

        created = json.loads(await create_task(title="Ship the release", owner=""))
        await assign_task(id=created["id"], to_handle="@bob")

        sent = fake.sent[0]
        assert "@bob" in sent.body
        assert created["id"] in sent.body

    async def test_arcstore_owner_write_happens_before_notify(
        self, notify_state: Any
    ) -> None:
        # The store write is observable via the tool's own return value
        # (result["owner_did"]) — if notify ran first and threw, a
        # write-after-notify implementation would surface the exception
        # before ever reaching the store, and this assertion would never
        # execute.
        from arcagent.modules.tasks.capabilities import assign_task, create_task

        st, fake, _bob_identity, bob_entity = notify_state
        await st.registry.register(bob_entity)

        created = json.loads(await create_task(title="Ship the release", owner=""))
        result = json.loads(await assign_task(id=created["id"], to_handle="@bob"))

        assert result["owner_did"] == bob_entity.did
        assert len(fake.sent) == 1

    async def test_notify_failure_does_not_roll_back_the_owner_write(
        self, notify_state: Any
    ) -> None:
        from arcagent.modules.tasks.capabilities import assign_task, create_task

        st, _fake, _bob_identity, bob_entity = notify_state
        await st.registry.register(bob_entity)
        st.messenger = _FakeMessenger(fail=True)

        created = json.loads(await create_task(title="Ship the release", owner=""))
        result = json.loads(await assign_task(id=created["id"], to_handle="@bob"))

        # The owner write is durable truth (arcstore) and must succeed
        # regardless of whether the notify envelope could be delivered.
        assert result["owner_did"] == bob_entity.did
        assert "error" not in result


@pytest.mark.asyncio
class TestAdoptHandlerStartsAssignedTask:
    """The assignee-side handler for a delivered ``task.assigned`` envelope.

    See the KNOWN CROSS-PHASE GAP note in the module docstring: this suite
    assumes ``start_task`` can adopt a task the caller already owns (not just
    an unowned one) — true once Phase C's handler AND the corresponding
    arcstore ``start_task`` fix both land.
    """

    async def test_handler_starts_the_task_named_in_the_envelope(
        self, notify_state: Any
    ) -> None:
        from arcteam.types import Message, MsgType

        from arcagent.modules.tasks import _runtime
        from arcagent.modules.tasks.capabilities import assign_task, create_task
        from arcagent.modules.tasks.handlers import handle_task_assigned

        st, _fake, bob_identity, bob_entity = notify_state
        await st.registry.register(bob_entity)

        created = json.loads(await create_task(title="Ship the release", owner=""))
        await assign_task(id=created["id"], to_handle="@bob")

        # Switch the runtime context to the assignee (bob) — same store and
        # registry, bob's own identity — simulating bob's own agent process
        # receiving the delivery.
        bob_state = _runtime._State(
            config=st.config,
            workspace=st.workspace,
            telemetry=st.telemetry,
            identity=bob_identity,
            registry=st.registry,
            store=st.store,
        )
        _runtime.bind(bob_state)

        envelope = Message(
            sender="agent://alice",
            to=["agent://bob"],
            msg_type=MsgType.TASK_ASSIGNED,
            body=f"@bob task_id={created['id']} — Ship the release",
        )
        result = json.loads(await handle_task_assigned(envelope))

        assert result["task"]["id"] == created["id"]
        assert result["task"]["status"] == "in_progress"
        assert result["task"]["owner_did"] == bob_identity.did

    async def test_handler_is_idempotent_on_redelivery(self, notify_state: Any) -> None:
        from arcteam.types import Message, MsgType

        from arcagent.modules.tasks import _runtime
        from arcagent.modules.tasks.capabilities import assign_task, create_task
        from arcagent.modules.tasks.handlers import handle_task_assigned

        st, _fake, bob_identity, bob_entity = notify_state
        await st.registry.register(bob_entity)

        created = json.loads(await create_task(title="Ship the release", owner=""))
        await assign_task(id=created["id"], to_handle="@bob")

        bob_state = _runtime._State(
            config=st.config,
            workspace=st.workspace,
            telemetry=st.telemetry,
            identity=bob_identity,
            registry=st.registry,
            store=st.store,
        )
        _runtime.bind(bob_state)

        envelope = Message(
            sender="agent://alice",
            to=["agent://bob"],
            msg_type=MsgType.TASK_ASSIGNED,
            body=f"@bob task_id={created['id']} — Ship the release",
        )
        first = json.loads(await handle_task_assigned(envelope))
        assert "error" not in first

        # A redelivered durable-consumer copy of the same envelope (e.g. after
        # a reconnect) must not error just because the task is now already
        # owned + in_progress under this same agent.
        second = json.loads(await handle_task_assigned(envelope))
        assert "error" not in second
        assert second["task"]["id"] == created["id"]
        assert second["task"]["status"] == "in_progress"

    async def test_handler_extracts_id_correctly_from_a_multi_mention_body(
        self, notify_state: Any
    ) -> None:
        # Regression guard for a naive "first token after @" parser — the id
        # must come from the explicit `task_id=` marker, not from proximity
        # to the mention.
        from arcteam.types import Message, MsgType

        from arcagent.modules.tasks import _runtime
        from arcagent.modules.tasks.capabilities import assign_task, create_task
        from arcagent.modules.tasks.handlers import handle_task_assigned

        st, _fake, bob_identity, bob_entity = notify_state
        await st.registry.register(bob_entity)

        created = json.loads(await create_task(title="Ship the release", owner=""))
        await assign_task(id=created["id"], to_handle="@bob")

        bob_state = _runtime._State(
            config=st.config,
            workspace=st.workspace,
            telemetry=st.telemetry,
            identity=bob_identity,
            registry=st.registry,
            store=st.store,
        )
        _runtime.bind(bob_state)

        envelope = Message(
            sender="agent://alice",
            to=["agent://bob"],
            msg_type=MsgType.TASK_ASSIGNED,
            body=f"cc @alice @bob task_id={created['id']} — Ship the release",
        )
        result = json.loads(await handle_task_assigned(envelope))
        assert result["task"]["id"] == created["id"]


class TestExtractTaskId:
    """Pure parsing unit — no runtime state needed."""

    def test_extracts_id_from_marker(self) -> None:
        from arcagent.modules.tasks.handlers import _extract_task_id

        body = "@bob task_id=task_abc123def456 — ship it"
        assert _extract_task_id(body) == "task_abc123def456"

    def test_returns_none_when_marker_absent(self) -> None:
        from arcagent.modules.tasks.handlers import _extract_task_id

        assert _extract_task_id("no marker here") is None
