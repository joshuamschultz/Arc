"""Dry run — prove a model-authored script before it is allowed to cost anything.

The script is executed by the *real* interpreter against a host that answers
plausibly and does nothing, so a run that would die on a typo, an unresolvable
name, a runaway loop or an over-budget fan-out dies here instead: for zero
tokens, zero child agents, and no file on disk. Every real execution passes
through this first, which is why the stub must be faithful rather than minimal —
a stub that answers more thinly than the real host would fail scripts that work,
and one that answers where the real host refuses would pass scripts that do not.

Validation always returns a verdict. Nothing raised by a bad script escapes to
the caller, because "this script is broken" is an ordinary answer here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from arcrun.dynamic.host import (
    DEFAULT_AGENT_CALLS,
    MAX_PARALLEL,
    AgentOutcome,
    AgentQuotaExceeded,
    AgentSpec,
    BudgetState,
    HostFailure,
    ScriptHost,
    ScriptOutcome,
)
from arcrun.dynamic.interpreter import execute_script

DRY_RUN_MAX_OPS = 100_000
"""Interpreter steps a dry run may take before it declares the script runaway."""

_STUB_TOKENS = 128
"""Plausible non-zero spend, so a script that scales on ``tokens_used`` still moves."""

_TERMINAL_OK = ("completed", "paused")
"""Endings that mean the script is sound. Pausing is a plan, not a failure."""

_SCHEMA_SAMPLES: dict[str, Any] = {
    "string": "stub",
    "integer": 1,
    "number": 1.0,
    "boolean": True,
    "array": ["stub"],
    "object": {},
}


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """The verdict on one script, and what reaching it revealed.

    ``phases`` and ``agent_calls`` are the operator-facing preview: what the
    script says it will do, and how many children it intends to spend doing it.
    """

    ok: bool
    error: str = ""
    outcome: ScriptOutcome | None = None
    agent_calls: int = 0
    phases: tuple[str, ...] = ()


def _sample_for_schema(schema: dict[str, Any]) -> Any:
    """Build a value matching ``schema`` so schema-reading scripts stay upright.

    A child given an ``output_schema`` returns that shape live, so a stub that
    returned its own generic mapping instead would fail exactly the scripts that
    were most careful about their contract.
    """
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return _SCHEMA_SAMPLES.get(str(schema.get("type", "object")), {})
    return {
        str(name): _sample_for_schema(spec) if isinstance(spec, dict) else "stub"
        for name, spec in properties.items()
    }


def _stub_output(spec: AgentSpec) -> dict[str, Any]:
    """The generic keys model-authored scripts reach for when reading a child."""
    described = spec.label or spec.prompt[:60]
    return {
        "summary": f"stub result for {described}",
        "text": f"stub result for {described}",
        "result": f"stub result for {described}",
        "label": spec.label,
        "items": ["stub item"],
        "findings": ["stub finding"],
        "score": 1,
    }


class StubHost:
    """A :class:`ScriptHost` that answers every call and performs no effect.

    It counts what a real run would have spent and remembers what the script
    said it was doing, so the report can preview both. Scratch lives in a dict:
    a validation pass that wrote a file would leave the real run a stale one.
    """

    __slots__ = ("_budget", "agent_calls", "logs", "phases", "scratch", "specs")

    def __init__(self, *, agent_call_budget: int = DEFAULT_AGENT_CALLS) -> None:
        self.agent_calls = 0
        self.phases: list[str] = []
        self.logs: list[str] = []
        self.scratch: dict[str, str] = {}
        self.specs: list[AgentSpec] = []
        self._budget = agent_call_budget

    def _charge(self, count: int) -> None:
        """Refuse what the real host would refuse, before pretending to do it."""
        if self.agent_calls + count > self._budget:
            raise AgentQuotaExceeded(
                f"script asked for {self.agent_calls + count} child runs, over its "
                f"agent-call budget of {self._budget}"
            )
        self.agent_calls += count

    async def spawn(self, spec: AgentSpec) -> AgentOutcome:
        """Answer one child run without starting one."""
        self._charge(1)
        self.specs.append(spec)
        output = (
            _sample_for_schema(spec.output_schema)
            if spec.output_schema is not None
            else _stub_output(spec)
        )
        return AgentOutcome(
            agent_id=f"stub-{self.agent_calls}",
            success=True,
            output=output,
            tokens_used=_STUB_TOKENS,
        )

    async def spawn_many(self, specs: list[AgentSpec]) -> list[AgentOutcome]:
        """Answer a batch in submission order, charging it as one fan-out."""
        if len(specs) > MAX_PARALLEL:
            raise HostFailure(
                f"parallel() was given {len(specs)} jobs, over the limit of {MAX_PARALLEL}"
            )
        self._charge(len(specs))
        outcomes: list[AgentOutcome] = []
        for offset, spec in enumerate(specs, start=len(self.specs) + 1):
            self.specs.append(spec)
            output = (
                _sample_for_schema(spec.output_schema)
                if spec.output_schema is not None
                else _stub_output(spec)
            )
            outcomes.append(
                AgentOutcome(
                    agent_id=f"stub-{offset}",
                    success=True,
                    output=output,
                    tokens_used=_STUB_TOKENS,
                )
            )
        return outcomes

    def phase(self, title: str) -> None:
        """Remember the stage name for the report's preview."""
        self.phases.append(title)

    def log(self, message: str) -> None:
        """Collect the progress line instead of showing it to an operator."""
        self.logs.append(message)

    def budget(self) -> BudgetState:
        """Report the same numbers a real run of this script would see."""
        return BudgetState(
            agent_calls_spent=self.agent_calls,
            agent_calls_total=self._budget,
            tokens_used=self.agent_calls * _STUB_TOKENS,
        )

    def scratch_write(self, name: str, content: str) -> str:
        """Hold working text in memory and return the path a real run would use."""
        self.scratch[name] = content
        return f"scratch/{name}"

    def scratch_read(self, name: str) -> str:
        """Read back what this dry run wrote; unwritten names read as empty."""
        return self.scratch.get(name, "")


async def dry_run(
    source: str,
    *,
    args: dict[str, Any] | None = None,
    agent_call_budget: int = DEFAULT_AGENT_CALLS,
) -> ValidationReport:
    """Execute ``source`` against a stubbed host and report whether it is sound.

    ``ok`` means the script reached a terminal state the strategy can act on —
    completing or pausing. Anything else, including a script that never says how
    it ends, comes back as ``ok=False`` with the text a model needs to repair it.
    """
    stub = StubHost(agent_call_budget=agent_call_budget)
    # Named as the Protocol so the stub's conformance is checked here, not
    # discovered when a real strategy swaps in the live host.
    host: ScriptHost = stub
    try:
        outcome = await execute_script(
            source,
            host=host,
            args=args,
            max_ops=DRY_RUN_MAX_OPS,
            agent_call_budget=agent_call_budget,
        )
    except Exception as exc:  # A broken script is a verdict, never a raise.
        return ValidationReport(
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            agent_calls=stub.agent_calls,
            phases=tuple(stub.phases),
        )
    ok = outcome.status in _TERMINAL_OK
    return ValidationReport(
        ok=ok,
        error="" if ok else (outcome.error or outcome.message or f"script {outcome.status}"),
        outcome=outcome,
        agent_calls=stub.agent_calls,
        phases=tuple(stub.phases),
    )


__all__ = [
    "DRY_RUN_MAX_OPS",
    "StubHost",
    "ValidationReport",
    "dry_run",
]
