"""SPEC-088 T-1353 (RED): intake rows ride the tasks dispatch loop (COMP-003, D-840).

An intake row is an ordinary arcstore task row written by ``POST /api/runs``
(SDD Data Model, "Intake task row"): ``id = "intake-<run_id>"``,
``max_attempts = 1``, ``tags = ["intake"]`` and

* ``metadata.intake = {run_id, run_purpose, correlation_id, callback_url,
  on_behalf_of, via_app}`` - control data the route minted, and
* ``metadata.app`` - opaque caller metadata.

Contract under test (``_dispatch_tick`` in ``capabilities.py``):

* the run is dispatched under ``metadata.intake.run_id`` (not a fresh uuid4),
  and the row's ``run_id`` is that same id;
* ``run_kwargs`` gain ``caller_did`` (``on_behalf_of`` when present, else the
  row's ``creator_did``) and ``run_purpose`` (forwarded verbatim);
* the prompt carries the task text and never the caller metadata or the
  correlation id (``_format_task_prompt`` is unchanged, REQ-596);
* a clean return settles a still-``in_progress`` intake row to ``done`` with
  ``output = {"text": result.content}``, bounded to 256 KiB;
* a crash dead-letters with no retry (one attempt);
* among equal-priority ready rows the earliest ``due_at`` dispatches first,
  rows with no ``due_at`` after every dated row (REQ-603); priority still wins.

The run callback is a recorder standing in for ``ArcAgent.run_collected``; it
accepts ``**kwargs`` so the new keyword arguments are observable rather than
crashing the call.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from arcrun import RunResult
from arctrust import AgentIdentity

_APP_DID = "did:arc:app:intake-client"
_PERSON_DID = "did:arc:user:alice"
_CORRELATION = "corr-ACME-PO-7731"
_APP_SECRET = "app-secret-value-0d1f"
_TASK_TEXT = "Summarize the open purchase orders for the north plant."
_OUTPUT_BOUND = 256 * 1024


class _RunRecorder:
    """Stand-in for ``ArcAgent.run_collected`` that records every call."""

    def __init__(self, result: Any = "ok", error: BaseException | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result = result
        self.error = error
        self.during: Any = None

    async def __call__(self, text: str, **kwargs: Any) -> Any:
        self.calls.append({"text": text, **kwargs})
        if self.during is not None:
            await self.during()
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def intake_state(tmp_path: Path, arcstore_opener: Any) -> Iterator[tuple[Any, AgentIdentity]]:
    """Runtime with a real in-memory store and dispatch ON."""
    from arcagent.modules.tasks import _runtime

    _runtime.reset()
    identity = AgentIdentity.generate(org="local", agent_type="agent")
    _runtime.configure(
        config={"enabled": True, "dispatch": True},
        telemetry=MagicMock(),
        workspace=tmp_path,
        identity=identity,
        arcstore_opener=arcstore_opener,
    )
    yield _runtime.state(), identity
    _runtime.reset()


async def _store(st: Any) -> Any:
    from arcagent.modules.tasks import _runtime

    await _runtime.ensure_store()
    return st.store


def _intake_row(
    owner_did: str,
    *,
    run_id: str | None = None,
    run_purpose: str = "manual",
    on_behalf_of: str | None = _PERSON_DID,
    creator_did: str = _APP_DID,
) -> Any:
    """Build the intake row exactly as the SDD Data Model describes it."""
    from arcagent.modules.tasks.models import Task

    pinned = run_id or str(uuid.uuid4())
    return Task(
        id=f"intake-{pinned}",
        title=_TASK_TEXT[:120],
        description=_TASK_TEXT,
        status="todo",
        owner_did=owner_did,
        creator_did=creator_did,
        max_attempts=1,
        tags=["intake"],
        metadata={
            "intake": {
                "run_id": pinned,
                "run_purpose": run_purpose,
                "correlation_id": _CORRELATION,
                "callback_url": "https://hooks.example.test/arc",
                "on_behalf_of": on_behalf_of,
                "via_app": _APP_DID if on_behalf_of else None,
            },
            "app": {"customer_ref": _APP_SECRET, "plant": "north-7"},
            "request_digest": "sha256:" + "c" * 64,
        },
    )


async def _seed_intake(st: Any, identity: AgentIdentity, **kwargs: Any) -> Any:
    store = await _store(st)
    return await store.create(_intake_row(identity.did, **kwargs))


@pytest.mark.asyncio
class TestIntakeRunIdentity:
    async def test_intake_dispatch_pins_run_id_and_keeps_metadata_out_of_prompt(
        self, intake_state: tuple[Any, AgentIdentity]
    ) -> None:
        from arcagent.modules.tasks.capabilities import _dispatch_tick

        st, identity = intake_state
        rec = _RunRecorder()
        st.agent_run_fn = rec
        pinned = str(uuid.uuid4())
        row = await _seed_intake(st, identity, run_id=pinned)

        await _dispatch_tick()

        assert len(rec.calls) == 1
        call = rec.calls[0]
        assert call["run_id"] == pinned, "intake run must use metadata.intake.run_id"
        stored = await st.store.get(row.id)
        assert stored is not None
        assert stored.run_id == pinned, "row run_id must link the pinned run"
        prompt = call["text"]
        assert _TASK_TEXT in prompt
        assert _APP_SECRET not in prompt, "caller metadata must never reach the prompt"
        assert "north-7" not in prompt
        assert _CORRELATION not in prompt, "correlation id must never reach the prompt"
        assert "hooks.example.test" not in prompt

    async def test_intake_dispatch_caller_did_is_on_behalf_of_when_present(
        self, intake_state: tuple[Any, AgentIdentity]
    ) -> None:
        from arcagent.modules.tasks.capabilities import _dispatch_tick

        st, identity = intake_state
        rec = _RunRecorder()
        st.agent_run_fn = rec
        await _seed_intake(st, identity, on_behalf_of=_PERSON_DID)

        await _dispatch_tick()

        assert len(rec.calls) == 1
        assert rec.calls[0].get("caller_did") == _PERSON_DID

    async def test_intake_dispatch_caller_did_falls_back_to_creator_did(
        self, intake_state: tuple[Any, AgentIdentity]
    ) -> None:
        from arcagent.modules.tasks.capabilities import _dispatch_tick

        st, identity = intake_state
        rec = _RunRecorder()
        st.agent_run_fn = rec
        await _seed_intake(st, identity, on_behalf_of=None, creator_did=_APP_DID)

        await _dispatch_tick()

        assert len(rec.calls) == 1
        assert rec.calls[0].get("caller_did") == _APP_DID

    @pytest.mark.parametrize("purpose", ["manual", "message"])
    async def test_intake_dispatch_forwards_run_purpose(
        self, intake_state: tuple[Any, AgentIdentity], purpose: str
    ) -> None:
        from arcagent.modules.tasks.capabilities import _dispatch_tick

        st, identity = intake_state
        rec = _RunRecorder()
        st.agent_run_fn = rec
        await _seed_intake(st, identity, run_purpose=purpose)

        await _dispatch_tick()

        assert len(rec.calls) == 1
        assert rec.calls[0].get("run_purpose") == purpose

    async def test_ordinary_row_keeps_fresh_run_id_and_no_intake_kwargs(
        self, intake_state: tuple[Any, AgentIdentity]
    ) -> None:
        """Guard: only a row with ``metadata.intake`` gets the intake treatment.

        A board task whose caller metadata merely *looks* like intake data
        (nested under ``metadata.app``) must not pin a caller-chosen run id or
        borrow a caller-chosen identity.
        """
        from arcagent.modules.tasks.capabilities import _dispatch_tick
        from arcagent.modules.tasks.models import Task

        st, identity = intake_state
        rec = _RunRecorder()
        st.agent_run_fn = rec
        store = await _store(st)
        spoof_run = str(uuid.uuid4())
        await store.create(
            Task(
                id="plain-1",
                title="Plain board task",
                status="todo",
                owner_did=identity.did,
                creator_did=_APP_DID,
                metadata={"app": {"intake": {"run_id": spoof_run, "on_behalf_of": _PERSON_DID}}},
            )
        )

        await _dispatch_tick()

        assert len(rec.calls) == 1
        assert rec.calls[0]["run_id"] != spoof_run
        assert "caller_did" not in rec.calls[0]
        assert "run_purpose" not in rec.calls[0]


@pytest.mark.asyncio
class TestIntakeSettlement:
    async def test_intake_clean_return_settles_done_with_output_text(
        self, intake_state: tuple[Any, AgentIdentity]
    ) -> None:
        from arcagent.modules.tasks.capabilities import _dispatch_tick

        st, identity = intake_state
        st.agent_run_fn = _RunRecorder(result=RunResult(content="Three open POs: 11, 12, 19."))
        row = await _seed_intake(st, identity)

        await _dispatch_tick()

        stored = await st.store.get(row.id)
        assert stored is not None
        assert stored.status == "done"
        assert stored.output == {"text": "Three open POs: 11, 12, 19."}
        assert stored.completed_at is not None

    async def test_intake_output_text_is_bounded_to_256_kib(
        self, intake_state: tuple[Any, AgentIdentity]
    ) -> None:
        from arcagent.modules.tasks.capabilities import _dispatch_tick

        st, identity = intake_state
        huge = "x" * (_OUTPUT_BOUND + 50_000)
        st.agent_run_fn = _RunRecorder(result=RunResult(content=huge))
        row = await _seed_intake(st, identity)

        await _dispatch_tick()

        stored = await st.store.get(row.id)
        assert stored is not None
        assert stored.status == "done"
        assert stored.output is not None
        text = stored.output["text"]
        assert len(text.encode("utf-8")) <= _OUTPUT_BOUND
        assert text.startswith("x" * 1000)

    async def test_intake_multibyte_output_bound_counts_bytes(
        self, intake_state: tuple[Any, AgentIdentity]
    ) -> None:
        """Adversarial: 256 KiB is a byte bound; 4-byte characters must not slip past it."""
        from arcagent.modules.tasks.capabilities import _dispatch_tick

        st, identity = intake_state
        huge = "\U0001f600" * (_OUTPUT_BOUND // 4 + 10_000)
        st.agent_run_fn = _RunRecorder(result=RunResult(content=huge))
        row = await _seed_intake(st, identity)

        await _dispatch_tick()

        stored = await st.store.get(row.id)
        assert stored is not None
        assert stored.status == "done"
        assert stored.output is not None
        assert len(stored.output["text"].encode("utf-8")) <= _OUTPUT_BOUND

    async def test_intake_agent_own_completion_wins_over_settle(
        self, intake_state: tuple[Any, AgentIdentity]
    ) -> None:
        """An agent that completed its own row during the run keeps its result."""
        from arcagent.modules.tasks.capabilities import _dispatch_tick

        st, identity = intake_state
        pinned = str(uuid.uuid4())
        rec = _RunRecorder(result=RunResult(content="loop final text"))
        row = await _seed_intake(st, identity, run_id=pinned)

        async def agent_completes_itself() -> None:
            await st.store.finish(
                row.id,
                status="done",
                resolution="agent finished",
                actor_did=identity.did,
                output={"text": "agent result"},
            )

        rec.during = agent_completes_itself
        st.agent_run_fn = rec

        await _dispatch_tick()

        assert rec.calls[0]["run_id"] == pinned
        stored = await st.store.get(row.id)
        assert stored is not None
        assert stored.status == "done"
        assert stored.resolution == "agent finished"
        assert stored.output == {"text": "agent result"}

    async def test_intake_crash_dead_letters_without_retry(
        self, intake_state: tuple[Any, AgentIdentity]
    ) -> None:
        from arcagent.modules.tasks.capabilities import _dispatch_tick

        st, identity = intake_state
        pinned = str(uuid.uuid4())
        rec = _RunRecorder(error=RuntimeError("provider exploded"))
        st.agent_run_fn = rec
        row = await _seed_intake(st, identity, run_id=pinned)

        await _dispatch_tick()

        assert [call["run_id"] for call in rec.calls] == [pinned]
        stored = await st.store.get(row.id)
        assert stored is not None
        assert stored.status == "failed", "one attempt only: a crash is terminal"
        assert stored.attempts == 1
        assert stored.next_attempt_at is None
        assert stored.last_error is not None and "provider exploded" in stored.last_error

        # A later tick must not run it again.
        await _dispatch_tick()
        assert len(rec.calls) == 1


@pytest.mark.asyncio
class TestDueAtOrdering:
    async def _seed_plain(
        self, st: Any, identity: AgentIdentity, task_id: str, *, due_at: str | None, priority: str
    ) -> None:
        from arcagent.modules.tasks.models import Task

        store = await _store(st)
        await store.create(
            Task(
                id=task_id,
                title=f"Task {task_id}",
                status="todo",
                priority=priority,
                owner_did=identity.did,
                creator_did=identity.did,
                due_at=due_at,
            )
        )

    async def test_equal_priority_dispatches_by_earliest_due_at_then_undated(
        self, intake_state: tuple[Any, AgentIdentity]
    ) -> None:
        from arcagent.modules.tasks.capabilities import _dispatch_tick

        st, identity = intake_state
        rec = _RunRecorder()
        st.agent_run_fn = rec
        base = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
        # Created oldest-first so the created_at tie-break alone would pick "undated".
        await self._seed_plain(st, identity, "undated", due_at=None, priority="medium")
        await self._seed_plain(
            st,
            identity,
            "due-late",
            due_at=(base + timedelta(days=2)).isoformat(),
            priority="medium",
        )
        await self._seed_plain(
            st, identity, "due-early", due_at=base.isoformat(), priority="medium"
        )

        order: list[str] = []
        for _ in range(3):
            await _dispatch_tick()
            running = await st.store.list(status="in_progress", owner_did=identity.did)
            assert len(running) == 1
            order.append(running[0].id)
            await st.store.finish(
                running[0].id, status="done", resolution="test", actor_did=identity.did
            )

        assert order == ["due-early", "due-late", "undated"]

    async def test_due_at_compares_instants_not_strings(
        self, intake_state: tuple[Any, AgentIdentity]
    ) -> None:
        """Adversarial: offsets differ, so string order and instant order disagree."""
        from arcagent.modules.tasks.capabilities import _dispatch_tick

        st, identity = intake_state
        st.agent_run_fn = _RunRecorder()
        # 08:00-05:00 is 13:00Z (later); 10:00+00:00 is 10:00Z (earlier). As
        # strings "2026-10-20T08..." sorts first, which is the wrong answer.
        await self._seed_plain(
            st, identity, "later-instant", due_at="2026-10-20T08:00:00-05:00", priority="medium"
        )
        await self._seed_plain(
            st, identity, "earlier-instant", due_at="2026-10-20T10:00:00+00:00", priority="medium"
        )

        await _dispatch_tick()

        running = await st.store.list(status="in_progress", owner_did=identity.did)
        assert [t.id for t in running] == ["earlier-instant"]

    async def test_priority_still_beats_due_at(
        self, intake_state: tuple[Any, AgentIdentity]
    ) -> None:
        """Guard: due_at is a tie-break after priority, never ahead of it."""
        from arcagent.modules.tasks.capabilities import _dispatch_tick

        st, identity = intake_state
        st.agent_run_fn = _RunRecorder()
        await self._seed_plain(
            st, identity, "medium-due", due_at="2026-10-11T00:00:00+00:00", priority="medium"
        )
        await self._seed_plain(st, identity, "high-undated", due_at=None, priority="high")

        await _dispatch_tick()

        running = await st.store.list(status="in_progress", owner_did=identity.did)
        assert [t.id for t in running] == ["high-undated"]
