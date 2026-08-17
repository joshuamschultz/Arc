"""Behaviour of the script interpreter.

The interpreter is the second half of the security boundary: the grammar decides
what may be written, this decides what actually happens when it runs. So these
tests care as much about the guards (op budget, type discipline, terminal
errors) as about the happy path.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Any

import pytest

from arcrun.dynamic.host import (
    MAX_HOST_CALLS,
    AgentOutcome,
    AgentSpec,
    BudgetExceeded,
    BudgetState,
    Cancelled,
)
from arcrun.dynamic.interpreter import (
    MAX_PHASES,
    ScriptError,
    _Interpreter,
    execute_script,
)
from arcrun.dynamic.journal import Journal, request_hash


class RecordingHost:
    """A host that records what it was asked to do and invents nothing."""

    def __init__(
        self,
        *,
        output: Any = None,
        fail_labels: frozenset[str] = frozenset(),
        raises: Exception | None = None,
    ) -> None:
        self.specs: list[AgentSpec] = []
        self.phases: list[str] = []
        self.logs: list[str] = []
        self.scratch: dict[str, str] = {}
        self._output = output if output is not None else {"ok": True}
        self._fail_labels = fail_labels
        self._raises = raises

    async def spawn(self, spec: AgentSpec) -> AgentOutcome:
        if self._raises is not None:
            raise self._raises
        self.specs.append(spec)
        succeeded = spec.label not in self._fail_labels
        return AgentOutcome(
            agent_id=f"child-{len(self.specs)}",
            success=succeeded,
            output=self._output if succeeded else None,
            tokens_used=7,
            error="" if succeeded else "child failed",
        )

    async def spawn_many(self, specs: list[AgentSpec]) -> list[AgentOutcome]:
        return [await self.spawn(spec) for spec in specs]

    def phase(self, title: str) -> None:
        self.phases.append(title)

    def log(self, message: str) -> None:
        self.logs.append(message)

    def budget(self) -> BudgetState:
        return BudgetState(agent_calls_spent=len(self.specs), agent_calls_total=32)

    def scratch_write(self, name: str, content: str) -> str:
        self.scratch[name] = content
        return f"scratch/{name}"

    def scratch_read(self, name: str) -> str:
        return self.scratch.get(name, "")


async def run(source: str, host: Any = None, **kwargs: Any) -> Any:
    """Execute ``source``, defaulting the host so tests stay about the script."""
    return await execute_script(source, host=host or RecordingHost(), **kwargs)


# --- terminating ------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_ends_the_run_and_carries_its_value() -> None:
    outcome = await run('complete({"answer": 42})')
    assert outcome.status == "completed"
    assert outcome.result == {"answer": 42}


@pytest.mark.asyncio
async def test_complete_returns_immediately_from_inside_a_loop() -> None:
    """A terminator deep in nested control flow must not run the remainder."""
    host = RecordingHost()
    outcome = await run(
        "for i in range(10):\n"
        "    if i == 2:\n"
        '        complete({"stopped_at": i})\n'
        '    log("iteration")\n',
        host,
    )
    assert outcome.result == {"stopped_at": 2}
    assert len(host.logs) == 2


@pytest.mark.asyncio
async def test_pause_is_a_legitimate_ending_not_a_failure() -> None:
    outcome = await run('pause("verification", "need a human")')
    assert outcome.status == "paused"
    assert outcome.kind == "verification"
    assert outcome.message == "need a human"
    assert outcome.is_resumable


@pytest.mark.asyncio
async def test_pause_refuses_a_kind_outside_the_known_set() -> None:
    outcome = await run('pause("whenever", "hm")')
    assert outcome.status == "failed"
    assert "whenever" in outcome.error


@pytest.mark.asyncio
async def test_a_script_that_never_terminates_itself_reports_failed() -> None:
    """Falling off the end means the author forgot to say what the answer was."""
    outcome = await run('log("did some work")')
    assert outcome.status == "failed"
    assert "complete" in outcome.error


# --- host calls -------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_reaches_the_host_and_returns_a_plain_mapping() -> None:
    host = RecordingHost(output={"claim": "x"})
    outcome = await run(
        'r = agent("investigate", {"label": "scout", "capability_mode": "read_only"})\n'
        "complete(r)\n",
        host,
    )
    assert host.specs[0].prompt == "investigate"
    assert host.specs[0].label == "scout"
    assert host.specs[0].capability_mode == "read_only"
    assert outcome.result["success"] is True
    assert outcome.result["output"] == {"claim": "x"}


@pytest.mark.asyncio
async def test_parallel_preserves_submission_order() -> None:
    host = RecordingHost()
    outcome = await run(
        "jobs = []\n"
        'for name in ["a", "b", "c"]:\n'
        '    jobs.append({"prompt": name, "label": name})\n'
        "results = parallel(jobs)\n"
        'complete([r["agent_id"] for r in results])\n',
        host,
    )
    assert [spec.label for spec in host.specs] == ["a", "b", "c"]
    assert outcome.result == ["child-1", "child-2", "child-3"]


@pytest.mark.asyncio
async def test_a_failed_child_is_a_value_the_script_decides_about() -> None:
    """Partial failure must never unwind the script — that is the author's call."""
    host = RecordingHost(fail_labels=frozenset({"b"}))
    outcome = await run(
        'jobs = [{"prompt": "x", "label": "a"}, {"prompt": "y", "label": "b"}]\n'
        "results = parallel(jobs)\n"
        'kept = [r for r in results if r["success"]]\n'
        'complete({"kept": len(kept), "total": len(results)})\n',
        host,
    )
    assert outcome.result == {"kept": 1, "total": 2}


@pytest.mark.asyncio
async def test_parallel_refuses_a_job_that_is_not_a_mapping() -> None:
    outcome = await run('parallel(["just a string"])')
    assert outcome.status == "failed"


@pytest.mark.asyncio
async def test_agent_refuses_an_unknown_option() -> None:
    """A typo in an option name must not be silently dropped."""
    outcome = await run('agent("go", {"capabilty_mode": "read_only"})')
    assert outcome.status == "failed"
    assert "capabilty_mode" in outcome.error


@pytest.mark.asyncio
async def test_phase_log_budget_and_scratch_reach_the_host() -> None:
    host = RecordingHost()
    outcome = await run(
        'phase("research")\n'
        'log("starting")\n'
        'scratch_write("notes.md", "hello")\n'
        "b = budget()\n"
        'complete({"notes": scratch_read("notes.md"), "total": b["agent_calls_total"]})\n',
        host,
    )
    assert host.phases == ["research"]
    assert host.logs == ["starting"]
    assert outcome.result == {"notes": "hello", "total": 32}
    assert outcome.phases_seen == ["research"]


# --- language ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_working_subset_of_the_language_evaluates() -> None:
    outcome = await run(
        "total = 0\n"
        "for n in range(5):\n"
        "    if n % 2 == 0:\n"
        "        total += n\n"
        "names = [w for w in ['a', 'bb', 'ccc'] if len(w) > 1]\n"
        'joined = "-".join(names)\n'
        'complete({"total": total, "joined": joined, "shout": "ab".upper()})\n'
    )
    assert outcome.result == {"total": 6, "joined": "bb-ccc", "shout": "AB"}


@pytest.mark.asyncio
async def test_f_strings_interpolate_because_prompts_are_built_from_data() -> None:
    host = RecordingHost()
    await run('topic = "batteries"\nagent(f"Research {topic} thoroughly")\ncomplete(1)\n', host)
    assert host.specs[0].prompt == "Research batteries thoroughly"


@pytest.mark.asyncio
async def test_args_carries_the_run_input() -> None:
    outcome = await run('complete(args["query"])', args={"query": "why"})
    assert outcome.result == "why"


@pytest.mark.asyncio
async def test_a_method_call_on_the_wrong_type_is_a_script_error() -> None:
    """The method table is typed, so ``upper`` on a list cannot dispatch."""
    outcome = await run("x = [1]\ncomplete(x.upper())")
    assert outcome.status == "failed"
    assert "upper" in outcome.error


@pytest.mark.asyncio
async def test_a_missing_key_fails_the_run_rather_than_returning_none() -> None:
    """Silently returning None would let a bad plan look like a good one."""
    outcome = await run('d = {"a": 1}\ncomplete(d["b"])')
    assert outcome.status == "failed"
    assert "b" in outcome.error


@pytest.mark.asyncio
async def test_division_by_zero_is_a_script_error_not_a_crash() -> None:
    outcome = await run("complete(1 / 0)")
    assert outcome.status == "failed"


# --- guards -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_infinite_loop_is_stopped_by_the_operation_budget() -> None:
    """Determinism does not prevent a runaway; the op counter does."""
    outcome = await run("while True:\n    x = 1\n", max_ops=5_000)
    assert outcome.status == "failed"
    assert "operation" in outcome.error


@pytest.mark.asyncio
async def test_the_agent_call_budget_stops_a_script_spawning_forever() -> None:
    host = RecordingHost()
    outcome = await run('while True:\n    agent("go")\n', host, agent_call_budget=3)
    assert outcome.status == "failed"
    assert len(host.specs) == 3


@pytest.mark.asyncio
async def test_a_string_cannot_be_grown_without_bound() -> None:
    """A memory bomb is as effective a denial of service as an infinite loop."""
    outcome = await run('s = "x"\nfor i in range(40):\n    s = s + s\n')
    assert outcome.status == "failed"
    assert "refused before" in outcome.error


@pytest.mark.parametrize(
    "source",
    [
        'complete(len("a" * 400000000))',
        "complete(len([0] * 40000000))",
        "complete(len(range(40000000)))",
        'complete(len(f"{0:400000000}"))',
        'complete(len(f"{1.0:.400000000f}"))',
        'complete(len(list("a" * 999999)))',
        'complete(len(sorted("a" * 999999)))',
        'complete(len(reversed("a" * 999999)))',
        'complete(len(enumerate("a" * 999999)))',
        'complete(len(("a" * 999999).replace("a", "bbbb")))',
    ],
)
@pytest.mark.asyncio
async def test_an_oversized_value_is_refused_before_it_is_allocated(source: str) -> None:
    """The one-shot form is the real attack, and the iterative form hid it.

    Growing a string by doubling trips on the second round, so the old test
    never exercised a single expression that asks for four hundred megabytes.
    Each of these built the value first and measured it after — peak memory of
    up to 1.6 GB, followed by a polite refusal. A guard that reports the size of
    a bomb it already detonated is not a guard.
    """
    outcome = await run(source)
    assert outcome.status == "failed"
    assert "refused before" in outcome.error, outcome.error


@pytest.mark.parametrize(
    "source",
    [
        'p = "x" * 999999\nl = [p] * 10000\ncomplete(len("".join(l)))',
        "a = [0] * 200\nb = [a] * 200\nc = [b] * 200\nd = [c] * 200\ncomplete(json_encode(d))",
        "a = [0] * 200\nb = [a] * 200\nc = [b] * 200\nd = [c] * 200\ncomplete(str(d))",
        'a = ["x" * 99999] * 5000\ncomplete(f"{a}")',
    ],
)
@pytest.mark.asyncio
async def test_aliasing_cannot_compose_legal_values_into_an_unbounded_one(source: str) -> None:
    """Every value here is individually legal; the whole is gigabytes.

    A per-value length check sees ``len == 200`` at each level and waves it
    through, so serialising has to price the whole structure — counting each
    distinct object once so aliasing is cheap to measure but not free to build.
    """
    outcome = await run(source)
    assert outcome.status == "failed"
    assert "refused before" in outcome.error, outcome.error


@pytest.mark.asyncio
async def test_a_value_that_contains_itself_is_named_not_a_stack_overflow() -> None:
    """Cycles are constructible, so measuring one has to terminate."""
    outcome = await run("jobs = []\njobs.append(jobs)\ncomplete(json_encode(jobs))")
    assert outcome.status == "failed"
    assert "contains itself" in outcome.error


@pytest.mark.asyncio
async def test_a_cancelled_host_ends_the_run_as_cancelled() -> None:
    """An operator kill must not be catchable or reportable as success."""
    host = RecordingHost(raises=Cancelled("operator stopped the run"))
    outcome = await run('agent("go")\ncomplete(1)\n', host)
    assert outcome.status == "cancelled"


@pytest.mark.asyncio
async def test_a_budget_breach_is_terminal_and_never_a_script_value() -> None:
    """The interpreter's own half of the contract, not the host's.

    A child that merely *fails* is an ordinary value the script decides about,
    so the risk is that a real breach arrives looking like one and the script
    carries on spending. This asserts the translation the interpreter owns: the
    run ends where the breach happened, the script's later statements never run,
    and nothing is reported as an answer. (That a breach is raised at all is the
    binding layer's contract and is covered there.)
    """
    host = RecordingHost(raises=BudgetExceeded("out of tokens"))
    outcome = await run(
        'r = agent("go")\nphase("kept going")\ncomplete({"salvaged": r["success"]})\n',
        host,
    )
    assert outcome.status == "budget_exceeded"
    assert outcome.is_resumable
    assert outcome.result is None, "a breach must not surface as the run's answer"
    assert outcome.phases_seen == [], "statements after the breach must not run"


@pytest.mark.asyncio
async def test_a_budget_breach_is_not_journaled_so_a_raised_ceiling_can_resume() -> None:
    """The call in flight must be re-issued live, not replayed as a canned
    failure — otherwise raising the ceiling and resuming fails identically
    forever."""
    journal = Journal()
    breached = RecordingHost(raises=BudgetExceeded("out of tokens"))
    outcome = await run('r = agent("go")\ncomplete(r)\n', breached, journal=journal)
    assert outcome.status == "budget_exceeded"
    assert [entry.kind for entry in journal.entries] == ["_provenance"]

    raised = RecordingHost()
    resumed = await run('r = agent("go")\ncomplete(r)\n', raised, journal=journal)
    assert resumed.status == "completed"
    assert len(raised.specs) == 1, "the breached call must be re-issued for real"


@pytest.mark.asyncio
async def test_a_script_outside_the_grammar_fails_before_the_host_is_touched() -> None:
    host = RecordingHost()
    outcome = await run('import os\nagent("go")\n', host)
    assert outcome.status == "failed"
    assert host.specs == []


@pytest.mark.asyncio
async def test_calling_a_bound_name_never_reaches_a_host_function() -> None:
    """The grammar refuses this now, so this holds the second layer honest.

    The dispatcher used to end in an unguarded ``scratch_read`` call with no
    check on the name, so ``x = "a"`` followed by ``x("a")`` read a scratch
    file. Widening the grammar again must not silently reopen that.
    """
    host = RecordingHost()
    host.scratch["a"] = "SECRET"
    interpreter = _Interpreter(
        source="",
        host=host,
        args={},
        journal=None,
        max_ops=100,
        agent_call_budget=1,
    )
    with pytest.raises(ScriptError) as excinfo:
        await interpreter._call_host("mystery", ["a"], {})
    assert "not a function" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_script_announcing_endless_phases_is_stopped() -> None:
    """``phase()`` skips the host-call ledger, so it needs its own ceiling."""
    outcome = await run(f'i = 0\nwhile i < {MAX_PHASES * 10}:\n    phase("p")\n    i = i + 1\n')
    assert outcome.status == "failed"
    assert str(MAX_PHASES) in outcome.error
    assert len(outcome.phases_seen) == MAX_PHASES


@pytest.mark.asyncio
async def test_a_script_logging_endlessly_is_stopped() -> None:
    host = RecordingHost()
    outcome = await run(
        f'i = 0\nwhile i < {MAX_HOST_CALLS * 5}:\n    log("p")\n    i = i + 1\n', host
    )
    assert outcome.status == "failed"
    assert len(host.logs) == MAX_HOST_CALLS


@pytest.mark.parametrize(
    "source",
    [
        'd = {}\nd[[1]] = 2\ncomplete("x")',
        'd = {}\ny = d[[1]]\ncomplete("x")',
        'd = {[1]: 2}\ncomplete("x")',
        'complete(f"{1.0:zz}")',
        'complete(int("9" * 4000) / 1.0)',
        "complete(" + "1 + " * 5000 + "1)",
    ],
)
@pytest.mark.asyncio
async def test_a_script_mistake_is_reported_never_raised(source: str) -> None:
    """``execute_script`` promises it does not raise for a script mistake.

    Each of these escaped as a raw Python exception, and the dynamic strategy
    calls ``execute_script`` with no guard — so any one of them killed the whole
    run instead of degrading to the ReAct fallback. The data-dependent form
    (``f"{1:{child_output}}"``) survives a dry run and only fires in production.
    """
    outcome = await run(source)
    assert outcome.status == "failed"
    assert outcome.error


@pytest.mark.asyncio
async def test_untrusted_child_output_steering_a_format_spec_is_reported() -> None:
    """The dry-run-surviving shape: the bad value comes from the child, not the script."""
    host = RecordingHost(output={"score": "zz"})
    outcome = await run("r = agent(\"score this\")\nlog(f\"{1:{r['output']['score']}}\")\n", host)
    assert outcome.status == "failed"
    assert "format" in outcome.error


# --- child specs ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_script_that_names_no_capability_mode_gets_the_narrow_one() -> None:
    """Naming nothing must not hand a child the parent's whole toolset (LLM06)."""
    host = RecordingHost()
    await run('agent("go")\ncomplete(1)\n', host)
    assert host.specs[0].capability_mode == "read_only"


@pytest.mark.parametrize("mode", ["ALL", "All", "", "everything", "readonly"])
@pytest.mark.asyncio
async def test_an_unrecognised_capability_mode_is_refused_not_quietly_narrowed(
    mode: str,
) -> None:
    """The option checker rejected unknown keys but accepted any value.

    A silently-corrected mode makes the script look like it constrained a child
    on purpose when it in fact hit a fallback.
    """
    host = RecordingHost()
    outcome = await run(f'agent("go", {{"capability_mode": "{mode}"}})\ncomplete(1)\n', host)
    assert outcome.status == "failed"
    assert "capability mode" in outcome.error
    assert host.specs == []


# --- journal provenance -----------------------------------------------------


PROVENANCE_SCRIPT = 'r = agent(args["task"])\ncomplete(r["agent_id"])\n'


@pytest.mark.asyncio
async def test_a_journal_cannot_replay_across_a_different_task() -> None:
    """``request_hash`` covers only each host call, so nothing bound a journal to
    its run: a second run given a materially different task spawned nothing,
    replayed the first run's answers, and reported ``completed``."""
    journal = Journal()
    first = await run(PROVENANCE_SCRIPT, args={"task": "AUDIT the Q1 filings"}, journal=journal)
    assert first.status == "completed"

    host = RecordingHost()
    second = await run(
        PROVENANCE_SCRIPT,
        host,
        args={"task": "DELETE the Q1 filings"},
        journal=journal,
    )
    assert second.status == "failed"
    assert "the task it was given has changed" in second.error
    assert host.specs == []


@pytest.mark.asyncio
async def test_a_journal_cannot_replay_under_a_different_script() -> None:
    """Seq 0 is the layer beneath the pinned script's signature.

    A pinned script is read from a workspace the agent can write, and the dry
    run checks form rather than intent — a substituted script that is
    grammatical passes it. The recorded source hash is what makes the swap
    visible at all.
    """
    journal = Journal()
    await run(PROVENANCE_SCRIPT, args={"task": "go"}, journal=journal)

    host = RecordingHost()
    substituted = (
        'jobs = [{"prompt": "exfiltrate", "capability_mode": "all"} for i in range(20)]\n'
        "parallel(jobs)\n"
        'complete("shipped")\n'
    )
    outcome = await run(substituted, host, args={"task": "go"}, journal=journal)
    assert outcome.status == "failed"
    assert "the script text" in outcome.error
    assert host.specs == [], "a substituted script must not reach the host at all"


@pytest.mark.asyncio
async def test_the_divergence_at_seq_zero_names_tampering_not_flakiness() -> None:
    """The generic journal wording reads as a flaky script.

    An operator seeing this line has to be able to tell "someone changed the
    script" apart from "this script is nondeterministic", because only one of
    those is an incident.
    """
    journal = Journal()
    await run(PROVENANCE_SCRIPT, args={"task": "go"}, journal=journal)

    outcome = await run(PROVENANCE_SCRIPT, args={"task": "something else"}, journal=journal)
    assert outcome.status == "failed"
    assert "tampering" in outcome.error
    assert "Nothing was replayed and nothing was executed" in outcome.error


@pytest.mark.asyncio
async def test_the_recorded_hash_is_over_the_exact_source_bytes() -> None:
    """A hash over a normalised form would agree with a script that is not the
    one being executed, which is the whole failure this guards against."""
    journal = Journal()
    await run(PROVENANCE_SCRIPT, args={"task": "go"}, journal=journal)

    recorded = journal.entries[0]
    assert recorded.kind == "_provenance"
    assert recorded.req_hash == request_hash(
        "_provenance",
        {
            "script": sha256(PROVENANCE_SCRIPT.encode("utf-8")).hexdigest(),
            "args": {"task": "go"},
        },
    )


@pytest.mark.parametrize(
    "edited",
    [
        PROVENANCE_SCRIPT + "\n",
        PROVENANCE_SCRIPT.replace("r =", "r  ="),
        PROVENANCE_SCRIPT.replace("\n", "\r\n"),
        "# a harmless-looking comment\n" + PROVENANCE_SCRIPT,
    ],
)
@pytest.mark.asyncio
async def test_even_a_whitespace_only_edit_to_a_pinned_script_diverges(edited: str) -> None:
    """Byte-exact, not semantically-equal: the guard cannot be argued out of."""
    journal = Journal()
    await run(PROVENANCE_SCRIPT, args={"task": "go"}, journal=journal)

    host = RecordingHost()
    outcome = await run(edited, host, args={"task": "go"}, journal=journal)
    assert outcome.status == "failed"
    assert host.specs == []


@pytest.mark.asyncio
async def test_the_same_script_and_input_still_replay_cleanly() -> None:
    """Provenance must cost nothing when the run really is the same run."""
    journal = Journal()
    first = await run(PROVENANCE_SCRIPT, args={"task": "go"}, journal=journal)

    host = RecordingHost()
    second = await run(PROVENANCE_SCRIPT, host, args={"task": "go"}, journal=journal)
    assert second.status == "completed"
    assert second.result == first.result
    assert host.specs == []
