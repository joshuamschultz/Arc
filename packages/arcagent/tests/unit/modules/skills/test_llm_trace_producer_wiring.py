"""H-041 producer wiring (the bug this closes): real runs must POPULATE ``llm_trace_id``.

The read-time curation join was built and unit-tested with a *hand-set* trace id, but no
production path ever set one — so a live operator opening curation found zero joinable
trace bodies (the producers-unwired pattern). These tests drive the REAL arcagent skill
hooks (``skills_llm_call_complete`` → ``skills_post_tool`` → ``skills_post_plan``) over a
REAL :class:`ArcSkillImprover` adapter and a REAL arcllm :class:`JSONLTraceStore`, and prove
the whole producer→store→join chain resolves an ACTUAL payload — never a hand-set id.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arcllm.trace_store import JSONLTraceStore, TraceRecord
from arcskill.improver import ArcSkillImprover, ImproverConfig, JoinedTrace
from arcskill.improver.trace_join import CurationUnavailable

from arcagent.modules.skills import _runtime
from arcagent.modules.skills.capabilities import (
    skills_llm_call_complete,
    skills_post_plan,
    skills_post_tool,
)

_SECRET_BODY = "Acme owes $42 for invoice #7."


class _Ctx:
    def __init__(self, **data: Any) -> None:
        self.data = data
        self.is_vetoed = False


@pytest.fixture(autouse=True)
def _clean_runtime() -> Any:
    _runtime.reset()
    yield
    _runtime.reset()


def _make_skill(root: Path) -> Path:
    skill = root / "skills" / "invoicer"
    (skill / "evals").mkdir(parents=True)
    skill_md = skill / "SKILL.md"
    skill_md.write_text("# invoicer\nSummarize an invoice.\n", encoding="utf-8")
    return skill_md


def _bind_real_improver(ws: Path, skill_md: Path) -> ArcSkillImprover:
    """Bind a REAL ArcSkillImprover as the skills-module adapter for the current task."""
    improver = ArcSkillImprover(
        ws,
        # optimize_after_uses high so observe/close never trips an improvement pass here.
        config=ImproverConfig(optimize_after_uses=999),
        tier="personal",
        skill_path=lambda _name: skill_md,
    )
    state = _runtime._State(adapter=improver, active=True, workspace=ws)
    # Index the skill the way agent:ready would, so the read-hook can activate it.
    state.skill_paths = {skill_md.resolve(): "invoicer"}
    _runtime.bind(state)
    return improver


async def _payload_source_over(store: JSONLTraceStore) -> Any:
    async def resolve(trace_id: str) -> dict[str, Any] | None:
        record = await store.get(trace_id)
        return record.model_dump() if record is not None else None

    return resolve


@pytest.mark.asyncio
async def test_real_run_populates_llm_trace_id_and_join_resolves_payload(tmp_path: Path) -> None:
    """The full producer→store→join path: a real arcllm payload becomes curatable.

    Drives the ACTUAL hooks — no ``observe(llm_trace_id=...)`` is ever called by hand.
    The id flows: arcllm records a trace → ``llm:call_complete`` stashes its id → the tool
    ``agent:post_tool`` forwards it → the span persists it → the read-time join resolves the
    real body from arcllm's store.
    """
    skill_md = _make_skill(tmp_path)
    ws = tmp_path / "ws"
    improver = _bind_real_improver(ws, skill_md)

    # A REAL arcllm trace, keyed by the same id arcllm records under.
    llm_store = JSONLTraceStore(ws / "agent_root")
    record = TraceRecord(
        provider="anthropic",
        model="claude",
        request_body={"messages": [{"role": "user", "content": "summarize invoice #7"}]},
        response_body={"text": _SECRET_BODY},
    )
    await llm_store.append(record)
    tid = record.trace_id

    # Drive the real within-turn hook order.
    await skills_post_tool(_Ctx(tool="read", args={"file_path": str(skill_md)}))  # activate skill
    await skills_llm_call_complete(_Ctx(**record.model_dump()))  # stash the arcllm trace id
    await skills_post_tool(_Ctx(tool="summarize", args={"invoice": "#7"}))  # forwards the id
    await skills_post_plan(_Ctx(task_outcome="success", turn_number=0, messages=[]))  # persist

    payload_source = await _payload_source_over(llm_store)
    joined = await improver.curatable_traces("invoicer", payload_source)
    visible = [j for j in joined if isinstance(j, JoinedTrace)]

    assert visible, "the used trace must be curatable — the producer populated llm_trace_id"
    # The producer wrote the id (not a hand-set value) — it came off the llm:call_complete event.
    assert tid in visible[0].span.llm_trace_ids
    # The read-time join resolved the ACTUAL arcllm payload body, not an empty.
    assert _SECRET_BODY in visible[0].body_text()

    await improver.aclose()
    await llm_store.close()


@pytest.mark.asyncio
async def test_no_llm_call_leaves_span_unlinked(tmp_path: Path) -> None:
    """A turn with no ``llm:call_complete`` links no id — declared unavailable, not silent.

    Guards the turn-scoped stash: without a stashed id the span carries no llm_trace_ids, so
    the join returns a reasoned :class:`CurationUnavailable` (never a fabricated id).
    """
    skill_md = _make_skill(tmp_path)
    ws = tmp_path / "ws"
    improver = _bind_real_improver(ws, skill_md)

    await skills_post_tool(_Ctx(tool="read", args={"file_path": str(skill_md)}))
    await skills_post_tool(_Ctx(tool="summarize", args={"invoice": "#7"}))  # no id stashed
    await skills_post_plan(_Ctx(task_outcome="success", turn_number=0, messages=[]))

    async def empty_source(_tid: str) -> dict[str, Any] | None:
        return None

    joined = await improver.curatable_traces("invoicer", empty_source)
    assert joined and all(isinstance(j, CurationUnavailable) for j in joined)
    assert "no linked LLM trace" in joined[0].reason

    await improver.aclose()


@pytest.mark.asyncio
async def test_trace_id_is_turn_scoped_and_cleared(tmp_path: Path) -> None:
    """The stashed id is turn-scoped: it clears at turn end so a later turn never inherits it."""
    skill_md = _make_skill(tmp_path)
    ws = tmp_path / "ws"
    _bind_real_improver(ws, skill_md)

    await skills_llm_call_complete(_Ctx(trace_id="abc123"))
    assert _runtime.state().current_llm_trace_id == "abc123"

    await skills_post_plan(_Ctx(task_outcome="success", turn_number=0, messages=[]))
    assert _runtime.state().current_llm_trace_id is None
