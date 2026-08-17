"""Bind the dynamic-script host surface to real child runs.

:mod:`arcrun.dynamic.host` says *what* a script may do; this module is the only
place that makes those calls actually happen. Everything here exists to keep a
child strictly smaller than its parent: a subset of the frozen tool set, one
level further from the depth ceiling, spending from a reservation taken before
the work starts, and sharing the parent's cancel event and event bus so an
operator kill reaches it and one hash chain records the whole run.

"Smaller" covers the controls as well as the tools. A child is *derived* from
the parent state, so the approval gate, the circuit-breaker thresholds and the
spend ceilings come with the tools they govern — a narrowed tool list whose
guards were left behind is not a narrowing at all (LLM06/ASI02).

The registry docstring names "a subagent with its own registry" as the
sanctioned route to dynamic capability. This is that route: the parent registry
is read, never mutated.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, TypeVar

import jsonschema

from arcrun._messages import content_text, system_message, user_message
from arcrun.dynamic.host import (
    MAX_PARALLEL,
    TERMINAL_ERRORS,
    AgentOutcome,
    AgentQuotaExceeded,
    AgentSpec,
    BudgetExceeded,
    BudgetState,
    Cancelled,
    HostError,
    HostFailure,
)
from arcrun.parallel_dispatch import dispatch_ready
from arcrun.registry import ToolRegistry
from arcrun.sandbox import Sandbox
from arcrun.state import RunState
from arcrun.strategies.react import check_breaker, react_loop
from arcrun.types import LoopResult, Tool

# A child gets the parent's tools or only its read-only ones. Any other value is
# treated as the narrower of the two: an unrecognised mode must never be read as
# "no restriction" (LLM06 excessive agency).
_ALL = "all"
_READ_ONLY = "read_only"

_JSON_FENCE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL)

# Anything outside this set is squeezed out of a label before it becomes part of
# a run id. A run id is read back in events, logs and outcomes; separators and
# newlines in one are a defect surface, not a feature.
_ID_UNSAFE = re.compile(r"[^\w.-]+")

_CHILD_FRAMING = (
    "You are a subagent handling one focused task inside a larger run.\n"
    "Complete only this task and state the result in your final message."
)
"""Framing appended to the parent's system text for every child.

Constant on purpose. ``label`` and ``phase`` are written by a model-authored
script — usually with an f-string over a previous child's output — and the
system message is instruction position, the one place such a string must never
land (LLM01/ASI06). They are display names, and they already ride the
``dynamic.agent.start`` event, which is where display names belong.
"""

_Number = TypeVar("_Number", int, float)


@dataclass
class _Ledger:
    """Agent-call accounting with an explicit reserved tranche.

    A fan-out reserves its whole cost before the first child starts, because
    half a batch is a worse answer than a refused one: the script would branch
    on results that were never going to arrive.
    """

    total: int
    spent: int = 0
    reserved: int = 0

    def reserve(self, count: int) -> None:
        """Claim ``count`` calls, or refuse the whole request."""
        if self.spent + self.reserved + count > self.total:
            raise AgentQuotaExceeded(
                f"agent-call budget exhausted: asked for {count}, "
                f"{max(0, self.total - self.spent - self.reserved)} of {self.total} left"
            )
        self.reserved += count

    def commit(self, count: int) -> None:
        """Turn a reservation into spend, once the child really starts."""
        self.reserved -= count
        self.spent += count

    def release(self, count: int) -> None:
        """Hand a reservation back for a child that never ran."""
        self.reserved -= count


@dataclass
class _Scratch:
    """Run-scoped working text, on disk when a directory was given.

    Names are filenames, never paths: the script is model-authored, so the only
    safe reading of ``../`` is "refuse" (ASI05).
    """

    directory: Path | None = None
    memory: dict[str, str] = field(default_factory=dict)

    def write(self, name: str, content: str) -> str:
        safe = self._checked(name)
        if self.directory is None:
            self.memory[safe] = content
            return safe
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / safe
        path.write_text(content, encoding="utf-8")
        return str(path)

    def read(self, name: str) -> str:
        safe = self._checked(name)
        if self.directory is None:
            if safe not in self.memory:
                raise HostFailure(f"scratch file not found: {name!r}")
            return self.memory[safe]
        path = self.directory / safe
        if not path.is_file():
            raise HostFailure(f"scratch file not found: {name!r}")
        return path.read_text(encoding="utf-8")

    @staticmethod
    def _checked(name: str) -> str:
        """Accept a bare filename; refuse anything that could name a path.

        The leading-dot rule covers ``.`` and ``..`` without a special case, and
        costs only the ability to name dotfiles — which a scratch pad never needs.
        """
        if not name or name.startswith("."):
            raise HostFailure(f"scratch name must be a simple filename, got {name!r}")
        if "/" in name or "\\" in name or Path(name).is_absolute():
            raise HostFailure(f"scratch name must not contain a path separator, got {name!r}")
        return name


class RunHost:
    """The real :class:`~arcrun.dynamic.host.ScriptHost`, backed by child runs.

    Args:
        model: Provider facade every child run invokes.
        state: The **parent** run's state — the source of lineage, depth,
            cancellation, the frozen tool set, and the usage counters children
            debit onto.
        sandbox: Permission boundary shared with every child.
        agent_call_budget: How many child runs this script may start in total.
        child_max_turns: Turn ceiling for a child that names none itself.
        scratch_dir: Where ``scratch_write`` lands. ``None`` keeps it in memory.
    """

    def __init__(
        self,
        *,
        model: Any,
        state: RunState,
        sandbox: Sandbox,
        agent_call_budget: int,
        child_max_turns: int = 8,
        scratch_dir: Path | None = None,
    ) -> None:
        self._model = model
        self._state = state
        self._sandbox = sandbox
        self._child_max_turns = child_max_turns
        self._ledger = _Ledger(total=agent_call_budget)
        self._scratch = _Scratch(directory=scratch_dir)

    # --- ScriptHost surface --------------------------------------------------

    async def spawn(self, spec: AgentSpec) -> AgentOutcome:
        """Run one child agent to completion and return its outcome."""
        self._refuse_if_terminal()
        self._ledger.reserve(1)
        return await self._spawn_reserved(spec)

    async def spawn_many(self, specs: list[AgentSpec]) -> list[AgentOutcome]:
        """Run a batch concurrently; outcomes come back in submission order."""
        self._refuse_if_terminal()
        if len(specs) > MAX_PARALLEL:
            raise HostFailure(
                f"parallel batch of {len(specs)} exceeds the ceiling of {MAX_PARALLEL}"
            )
        if not specs:
            return []
        self._ledger.reserve(len(specs))
        results = await dispatch_ready(
            specs,
            self._spawn_reserved,
            max_parallel=self._state.max_parallel,
        )
        return [self._as_outcome(spec, result) for spec, result in zip(specs, results, strict=True)]

    def phase(self, title: str) -> None:
        """Name the stage now starting, for progress display only."""
        self._state.event_bus.emit("dynamic.phase", {"title": title})

    def log(self, message: str) -> None:
        """Emit a progress line to the operator."""
        self._state.event_bus.emit("dynamic.log", {"message": message})

    def budget(self) -> BudgetState:
        """Report spend so a script can scale its own depth."""
        return BudgetState(
            agent_calls_spent=self._ledger.spent,
            agent_calls_total=self._ledger.total,
            tokens_used=self._state.tokens_used["total"],
        )

    def scratch_write(self, name: str, content: str) -> str:
        """Persist run-scoped working text; returns the path written."""
        return self._scratch.write(name, content)

    def scratch_read(self, name: str) -> str:
        """Read back run-scoped working text."""
        return self._scratch.read(name)

    # --- child runs ----------------------------------------------------------

    def _terminal_error(self) -> HostError | None:
        """The parent-run condition that must end this script now, if any.

        Spend goes through the loop's own :func:`check_breaker` so a script and
        an ordinary turn halt on exactly one rule with one owner. ``_debit`` has
        already rolled every finished child's usage onto the parent, so the
        counters read here are the whole run's — without this the only bound on
        a script is ``agent_call_budget x child_max_turns`` (LLM10).
        """
        if self._state.cancel_event.is_set():
            return Cancelled("run cancelled; no further child agents will start")
        breach = check_breaker(self._state)
        if breach is None:
            return None
        return BudgetExceeded(f"run halted: {breach}")

    def _refuse_if_terminal(self) -> None:
        terminal = self._terminal_error()
        if terminal is not None:
            raise terminal

    async def _spawn_reserved(self, spec: AgentSpec) -> AgentOutcome:
        """Run one child against a reservation already taken for it."""
        # Re-read per child, not once per batch: a cancel or a breach landing
        # mid fan-out must stop the children still queued behind the
        # concurrency limit instead of waiting for the whole batch to drain.
        terminal = self._terminal_error()
        if terminal is not None:
            self._ledger.release(1)
            raise terminal
        depth = self._state.depth + 1
        agent_id = f"{_id_slug(spec.label)}-{uuid.uuid4().hex[:12]}"
        if depth > self._state.max_depth:
            self._ledger.release(1)
            return AgentOutcome(
                agent_id=agent_id,
                success=False,
                output=None,
                error=f"child depth {depth} exceeds max_depth {self._state.max_depth}",
            )
        self._ledger.commit(1)
        return await self._run_child(spec, agent_id, depth)

    async def _run_child(self, spec: AgentSpec, agent_id: str, depth: int) -> AgentOutcome:
        """Build the child state, run the loop, then debit and shape the result."""
        child = self._build_child_state(spec, agent_id, depth)
        bus = self._state.event_bus
        bus.emit(
            "dynamic.agent.start",
            {
                "run_id": agent_id,
                "parent_run_id": self._state.run_id,
                "depth": depth,
                "label": spec.label,
                "phase": spec.phase,
                "capability_mode": spec.capability_mode,
            },
        )
        turns = spec.max_turns or self._child_max_turns
        try:
            result = await react_loop(self._model, child, self._sandbox, turns)
            if spec.output_schema is not None:
                result = await self._enforce_output_contract(child, spec, result, turns)
        finally:
            # A child that died partway still burned tokens; the parent's
            # breaker has to see them or the run under-counts its own spend.
            self._debit(child)
        outcome = _outcome_from_result(agent_id, child, result, spec.output_schema)
        bus.emit(
            "dynamic.agent.end",
            {
                "run_id": agent_id,
                "success": outcome.success,
                "cancelled": outcome.cancelled,
                "tokens_used": outcome.tokens_used,
                "error": outcome.error,
            },
        )
        return outcome

    def _build_child_state(self, spec: AgentSpec, agent_id: str, depth: int) -> RunState:
        """Derive the child from the parent, overriding only what must differ.

        Derived rather than assembled from a field list, because a list drops
        every field nobody remembered to add to it and lets ``RunState``'s
        permissive defaults stand in — which is how a child came to run the
        parent's dangerous tools with the human-approval gate, the circuit
        breakers and the spend ceilings all absent. Deriving makes a field added
        to ``RunState`` later inherit by default; the overrides below are the
        whole of what a child owns for itself.

        The child never inherits the parent's accumulated turns: a subagent
        exists precisely so a focused task does not carry the whole run's
        context, and an inherited transcript is also an inherited injection
        surface (LLM01).
        """
        prompt = spec.prompt
        if spec.output_schema is not None:
            prompt = f"{prompt}\n\n{_output_contract(spec.output_schema)}"
        parent = self._state
        return replace(
            parent,
            messages=[
                system_message(f"{self._parent_system_text()}\n\n{_CHILD_FRAMING}"),
                user_message(prompt),
            ],
            registry=self._child_registry(spec.capability_mode),
            run_id=agent_id,
            parent_run_id=parent.run_id,
            depth=depth,
            # A child starts at zero usage, so its ceilings are what the parent
            # has left rather than the parent's raw ceiling — otherwise every
            # child of a fan-out is handed the whole budget again (LLM10).
            turn_count=0,
            tokens_used={"input": 0, "output": 0, "total": 0},
            cost_usd=0.0,
            tool_calls_made=0,
            max_tokens=_headroom(parent.max_tokens, parent.tokens_used["total"]),
            max_cost_usd=_headroom(parent.max_cost_usd, parent.cost_usd),
            # Breaker thresholds inherit; their running counters do not — a
            # child issues a fresh chain of calls, not a continuation of one.
            runaway_signature=None,
            runaway_count=0,
            consecutive_tool_errors=0,
            # An operator steers the run they are watching. A child sharing
            # these queues would swallow a message meant for the parent.
            steer_queue=asyncio.Queue(maxsize=parent.steer_queue.maxsize),
            followup_queue=asyncio.Queue(maxsize=parent.followup_queue.maxsize),
            completion_payload=None,
            completion_tool=None,
            # Both hooks are bound to the parent's transcript and session: the
            # context hook would rewrite a list it was never written for, and a
            # child checkpoint would overwrite the session's resume point with a
            # run that session does not hold.
            transform_context=None,
            on_checkpoint=None,
        )

    def _parent_system_text(self) -> str:
        """The parent's system words, so a child inherits the same posture."""
        return "\n\n".join(
            content_text(message.content)
            for message in self._state.messages
            if getattr(message, "role", "") == "system"
        )

    def _child_registry(self, capability_mode: str) -> ToolRegistry:
        """A frozen subset of the parent's tools — narrowing only.

        Built by reading the parent registry, never by mutating it: the parent's
        set is frozen for the run and its byte-stability is the provider cache
        prefix.
        """
        keep_all = capability_mode == _ALL
        tools: list[Tool] = []
        for name in self._state.registry.names():
            tool = self._state.registry.get(name)
            if tool is None:
                continue
            if keep_all or tool.classification == _READ_ONLY:
                tools.append(tool)
        registry = ToolRegistry(tools=tools, event_bus=self._state.event_bus)
        registry.freeze()
        return registry

    def _debit(self, child: RunState) -> None:
        """Roll a child's usage onto the parent so its breaker sees the truth."""
        for key in ("input", "output", "total"):
            self._state.tokens_used[key] += child.tokens_used[key]
        self._state.cost_usd += child.cost_usd

    async def _enforce_output_contract(
        self,
        child: RunState,
        spec: AgentSpec,
        result: LoopResult,
        turns: int,
    ) -> LoopResult:
        """Validate the child's fenced JSON block, correcting it at most once.

        The contract rides the prompt rather than a provider's structured-output
        feature so it holds on every model. One retry, then the child reports an
        honest failure — a model that misses the shape twice will usually miss
        it a third time, and the loop is not the place to keep paying for that.
        """
        violation = _contract_violation(result.content, spec.output_schema)
        if violation is None:
            return result
        child.messages.append(
            user_message(
                f"Your reply did not satisfy the output contract: {violation}\n\n"
                f"{_output_contract(spec.output_schema)}"
            )
        )
        self._state.event_bus.emit(
            "dynamic.agent.contract_retry",
            {"run_id": child.run_id, "violation": violation},
        )
        return await react_loop(self._model, child, self._sandbox, child.turn_count + turns)

    def _as_outcome(self, spec: AgentSpec, result: AgentOutcome | BaseException) -> AgentOutcome:
        """Turn a batch slot into an outcome, keeping siblings independent.

        A child that raised is that child's failure, not the batch's: the script
        decides what a partial fan-out means. The exception is
        :data:`~arcrun.dynamic.host.TERMINAL_ERRORS` — a cancel or a breached
        budget ends the run outright, and letting one back as an ordinary
        ``success=False`` value would hand the script something to branch on
        after the operator or the ceiling already said stop.
        """
        if isinstance(result, AgentOutcome):
            return result
        if isinstance(result, TERMINAL_ERRORS):
            raise result
        return AgentOutcome(
            agent_id=f"{_id_slug(spec.label)}-failed",
            success=False,
            output=None,
            error=f"{type(result).__name__}: {result}",
        )


def _id_slug(label: str) -> str:
    """A bounded, charset-clean stem for a child's run id."""
    return _ID_UNSAFE.sub("-", label)[:32].strip("-") or "agent"


def _headroom(ceiling: _Number | None, spent: _Number) -> _Number | None:
    """What is left of a spend ceiling, or ``None`` when there is no ceiling."""
    if ceiling is None:
        return None
    return max(ceiling - spent, 0)


def _output_contract(schema: dict[str, Any] | None) -> str:
    """The instruction that makes a child's answer machine-readable."""
    return (
        "End your final message with a fenced code block tagged json, containing "
        "an object that satisfies this JSON schema and nothing else:\n"
        f"```json\n{json.dumps(schema, indent=2, sort_keys=True)}\n```"
    )


def _contract_violation(content: str | None, schema: dict[str, Any] | None) -> str | None:
    """Describe how the reply broke the output contract, or ``None`` if it held."""
    match = _JSON_FENCE.search(content or "")
    if match is None:
        return "no fenced ```json block was present"
    try:
        parsed = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        return f"the fenced block was not valid JSON — {exc}"
    try:
        jsonschema.validate(parsed, schema)
    except jsonschema.ValidationError as exc:
        return f"the object did not match the schema — {exc.message}"
    except jsonschema.SchemaError as exc:
        return f"the requested schema is itself invalid — {exc.message}"
    return None


def _parsed_output(content: str | None) -> Any:
    """The validated object carried by a reply that already passed the contract."""
    match = _JSON_FENCE.search(content or "")
    return json.loads(match.group(1)) if match is not None else None


def _outcome_from_result(
    agent_id: str,
    child: RunState,
    result: LoopResult,
    schema: dict[str, Any] | None,
) -> AgentOutcome:
    """Shape a finished child run as the value the script sees."""
    payload = result.completion_payload or {}
    error = str(payload.get("error") or "")
    cancelled = error == "cancelled"
    tokens = child.tokens_used["total"]
    if schema is not None:
        violation = _contract_violation(result.content, schema)
        if violation is not None:
            return AgentOutcome(
                agent_id=agent_id,
                success=False,
                output=result.content,
                cancelled=cancelled,
                tokens_used=tokens,
                error=f"output contract not satisfied: {violation}",
            )
        return AgentOutcome(
            agent_id=agent_id,
            success=not error,
            output=_parsed_output(result.content),
            cancelled=cancelled,
            tokens_used=tokens,
            error=error,
        )
    success = not error and payload.get("status", "success") != "failed"
    return AgentOutcome(
        agent_id=agent_id,
        success=success,
        output=result.content,
        cancelled=cancelled,
        tokens_used=tokens,
        error=error or ("" if success else str(payload.get("summary", ""))),
    )


__all__ = ["RunHost"]
