"""Item 20 P20-2 — causal attribution for audit events (arctrust.causal).

Who caused an audited act is set by code (dispatch, middleware, scheduler) on a
ContextVar, stamped onto every ``AuditEvent`` at construction, and never
inherited by detached background work.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from arctrust import causal
from arctrust.audit import AuditEvent
from arctrust.causal import CausalContext


def _root(
    initiator: causal.Initiator = "agent", initiator_id: str = "did:arc:t:a/1"
) -> CausalContext:
    return causal.root(initiator, initiator_id)


class TestBinding:
    def test_unbound_is_none_and_actor_is_loudly_unattributed(self) -> None:
        assert causal.current() is None
        assert causal.actor_did() == causal.UNATTRIBUTED

    def test_bind_sets_and_restores(self) -> None:
        ctx = _root()
        with causal.bind(ctx) as bound:
            assert bound is ctx
            assert causal.current() is ctx
            assert causal.actor_did() == "did:arc:t:a/1"
        assert causal.current() is None

    def test_root_mints_a_request_id(self) -> None:
        assert _root().request_id != _root().request_id

    def test_refine_adds_ids_but_keeps_the_initiator(self) -> None:
        with causal.bind(_root()), causal.refine(run_id="r1", tool_call_id="t1") as child:
            assert child.initiator == "agent"
            assert child.run_id == "r1"
            assert child.tool_call_id == "t1"
            assert causal.current() is child
        assert causal.current() is None

    def test_refine_cannot_change_who_initiated(self) -> None:
        with causal.bind(_root()), pytest.raises(TypeError):
            with causal.refine(initiator="operator"):
                pass
        with causal.bind(_root()), pytest.raises(TypeError):
            with causal.refine(initiator_id="did:arc:evil"):
                pass

    def test_refine_outside_any_binding_fails_loud(self) -> None:
        with pytest.raises(LookupError):
            with causal.refine(run_id="r1"):
                pass

    def test_delegate_records_on_behalf_of_and_keeps_correlation(self) -> None:
        ui = causal.root("ui_session", "did:arc:user:josh")
        with causal.bind(ui), causal.refine(task_id="task-9"):
            with causal.delegate("agent", "did:arc:t:a/1", run_id="r1") as acted:
                assert acted.initiator == "agent"
                assert acted.initiator_id == "did:arc:t:a/1"
                assert acted.on_behalf_of == "did:arc:user:josh"
                assert acted.task_id == "task-9"
                assert acted.run_id == "r1"
                assert acted.request_id == ui.request_id

    def test_delegate_without_a_parent_is_a_root(self) -> None:
        with causal.delegate("agent", "did:arc:t:a/1", run_id="r1") as acted:
            assert acted.on_behalf_of is None
            assert acted.run_id == "r1"


class TestValidation:
    def test_unknown_fields_are_refused(self) -> None:
        with pytest.raises(ValidationError):
            CausalContext.model_validate(
                {"initiator": "agent", "initiator_id": "x", "request_id": "r", "role": "admin"}
            )

    def test_unknown_initiator_kinds_are_refused(self) -> None:
        with pytest.raises(ValidationError):
            causal.root("god", "did:arc:x")  # type: ignore[arg-type]  # reason: the forgery under test

    def test_control_characters_are_refused(self) -> None:
        # A newline in an id would forge a second log line in any text sink.
        with pytest.raises(ValidationError):
            causal.root("agent", "did:arc:a\nactor_did=did:arc:operator")

    def test_frozen(self) -> None:
        ctx = _root()
        with pytest.raises(ValidationError):
            ctx.run_id = "r2"  # type: ignore[misc]  # reason: the mutation under test


class TestAuditEventStamping:
    def test_event_carries_the_bound_context(self) -> None:
        with causal.bind(_root()), causal.refine(run_id="r1", tool_call_id="t1"):
            event = AuditEvent(actor_did="did:arc:t:a/1", action="x", target="y", outcome="ok")
        assert event.causal is not None
        assert event.causal.run_id == "r1"
        assert event.causal.tool_call_id == "t1"

    def test_event_outside_any_binding_has_no_context(self) -> None:
        event = AuditEvent(actor_did="a", action="x", target="y", outcome="ok")
        assert event.causal is None

    def test_causal_survives_a_json_round_trip(self) -> None:
        with causal.bind(_root()), causal.refine(run_id="r1"):
            event = AuditEvent(actor_did="a", action="x", target="y", outcome="ok")
        restored = AuditEvent.model_validate(event.model_dump(mode="json"))
        assert restored.causal == event.causal


class TestDetachedWork:
    async def test_spawn_detached_never_inherits_the_request(self) -> None:
        """Background work started inside a request is its own root.

        Forced interleaving: the foreground holds its binding open until the
        background task has read its context, so an inherited binding would be
        observed rather than raced past.
        """
        seen: list[CausalContext | None] = []
        started = asyncio.Event()
        release = asyncio.Event()

        async def background() -> None:
            seen.append(causal.current())
            started.set()
            await release.wait()
            seen.append(causal.current())

        with (
            causal.bind(causal.root("ui_session", "did:arc:user:josh")),
            causal.refine(run_id="R"),
        ):
            task = causal.spawn_detached(background(), initiator_id="did:arc:system:digest")
            await started.wait()
            assert causal.current() is not None
            release.set()
            await task

        assert all(ctx is not None for ctx in seen)
        for ctx in seen:
            assert ctx is not None
            assert ctx.initiator == "system"
            assert ctx.initiator_id == "did:arc:system:digest"
            assert ctx.run_id is None
            assert ctx.on_behalf_of is None

    async def test_plain_create_task_would_have_inherited(self) -> None:
        """The control: asyncio copies the context, which is the bug detach fixes."""
        seen: list[CausalContext | None] = []

        async def background() -> None:
            seen.append(causal.current())

        with causal.bind(causal.root("ui_session", "did:arc:user:josh")):
            await asyncio.create_task(background())
        assert seen[0] is not None and seen[0].initiator == "ui_session"

    async def test_a_cancelled_request_does_not_leak_into_the_next(self) -> None:
        gate = asyncio.Event()

        async def request(initiator_id: str) -> CausalContext | None:
            with causal.bind(causal.root("ui_session", initiator_id)):
                await gate.wait()
            return causal.current()

        first = asyncio.create_task(request("did:arc:user:a"))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert causal.current() is None

        second = asyncio.create_task(request("did:arc:user:b"))
        gate.set()
        assert await second is None
