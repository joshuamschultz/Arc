"""Evaluates a validated script against a host.

The grammar (:mod:`arcrun.dynamic.grammar`) decides what may be written; this
decides what happens when it runs. Three properties matter and each is enforced
here rather than trusted:

* **Bounded.** Every node evaluated spends one operation from a fixed budget, so
  a runaway loop ends in a typed error instead of a hung run. Values are size
  capped for the same reason — a doubling string is as effective a denial of
  service as ``while True``.
* **Typed.** Method calls dispatch through an explicit table keyed on the
  receiver's built-in type. ``getattr`` is never used, so a script cannot reach
  a Python object graph even by accident.
* **Deterministic.** Nothing here reads a clock, a random source, or the
  environment. That is what makes journal replay sound: a resumed run walks the
  same script and gets the same recorded host results.

Failure has three distinct shapes and they must not be conflated. A script
mistake is a ``failed`` outcome the author can fix. An operator cancel or a
budget breach is terminal and uncatchable. A child agent that fails is neither —
it is an ordinary value the script decides what to do about.
"""

from __future__ import annotations

import ast
import json
import re
from hashlib import sha256
from typing import Any, Final, Protocol, TypeVar, get_args

from arcrun.dynamic.grammar import ScriptSyntaxError, parse_script
from arcrun.dynamic.host import (
    DEFAULT_AGENT_CALLS,
    MAX_HOST_CALLS,
    MAX_PARALLEL,
    AgentQuotaExceeded,
    AgentSpec,
    BudgetExceeded,
    Cancelled,
    HostError,
    PauseKind,
    ScriptHost,
    ScriptOutcome,
)
from arcrun.dynamic.journal import JournalDivergence, JournalError, request_hash

MAX_OPS: Final = 1_000_000
"""Operations one script may spend. The ceiling on a runaway loop."""

MAX_STRING_LENGTH: Final = 1_000_000
"""Characters in any single string a script builds."""

MAX_CONTAINER_LENGTH: Final = 10_000
"""Items in any single list or mapping a script builds."""

MAX_PHASES: Final = 64
"""Phase titles one run may announce.

``phase()`` bypasses the host-call ledger, so without its own ceiling a loop
could copy tens of thousands of titles into the outcome and the completion
event. More than this is not describing phases.
"""

MAX_VALUE_DEPTH: Final = 64
"""How deep a value may nest before it is refused as unmeasurable.

``l = [l]`` in a loop builds a value no single guard sees growing, and walking
it to serialise it would exhaust the stack.
"""

_PAUSE_KINDS: Final = frozenset(get_args(PauseKind))
"""Derived from the host's ``PauseKind`` so the two cannot drift apart."""

_CAPABILITY_MODES: Final = frozenset({"all", "read_only"})
"""What ``capability_mode`` may say. Anything else is a refusal, not a default."""

_DEFAULT_CAPABILITY_MODE: Final = "read_only"
"""A script that names no capability mode gets the narrow one (LLM06)."""

_AGENT_OPTIONS: Final = frozenset(
    {"prompt", "label", "capability_mode", "output_schema", "max_turns", "phase"}
)

_SPEC_NUMBERS: Final = re.compile(r"\d+")
"""Width and precision inside a format spec — both multiply output length."""


class ScriptError(Exception):
    """The script did something it cannot recover from. The author's bug."""


class _Complete(Exception):  # noqa: N818 — control-flow signal, not an error
    """Non-local exit carrying the run's answer."""

    def __init__(self, value: Any) -> None:
        super().__init__("complete")
        self.value = value


class _Pause(Exception):  # noqa: N818 — control-flow signal, not an error
    """Non-local exit asking for the run to be resumed later."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__("pause")
        self.kind = kind
        self.message = message


class _Break(Exception):  # noqa: N818 — control-flow signal, not an error
    """Loop control. Never escapes the loop that catches it."""


class _Continue(Exception):  # noqa: N818 — control-flow signal, not an error
    """Loop control. Never escapes the loop that catches it."""


class JournalPort(Protocol):
    """The slice of the journal the interpreter needs.

    Stated as a Protocol so the engine depends on replay behaviour rather than
    on a concrete store — a caller with no durable home passes nothing at all.
    """

    def replay(self, seq: int, kind: str, req_hash: str) -> tuple[bool, Any]:
        """Return ``(True, result)`` for an already-performed call."""
        ...

    def record(self, seq: int, kind: str, req_hash: str, result: Any) -> None:
        """Append the result of a call just performed."""
        ...

    def __len__(self) -> int:
        """How many calls are already recorded."""
        ...


async def execute_script(
    source: str,
    *,
    host: ScriptHost,
    args: dict[str, Any] | None = None,
    journal: JournalPort | None = None,
    max_ops: int = MAX_OPS,
    agent_call_budget: int = DEFAULT_AGENT_CALLS,
) -> ScriptOutcome:
    """Run ``source`` against ``host`` and report how it ended.

    Args:
        source: The model-authored script.
        host: The only surface the script can affect.
        args: Run input, bound as the script's ``args`` name.
        journal: When given, already-performed host calls replay from it instead
            of running again, which is what makes a paused run resumable.
        max_ops: Operation ceiling for the whole run.
        agent_call_budget: How many child agents the script may start.

    Returns:
        A :class:`ScriptOutcome`. This function does not raise for a script
        mistake; it reports it, because a bad script is an expected result when
        the author is a language model.
    """
    try:
        module = parse_script(source)
    except ScriptSyntaxError as exc:
        return ScriptOutcome(status="failed", error=str(exc))

    interpreter = _Interpreter(
        source=source,
        host=host,
        args=args or {},
        journal=journal,
        max_ops=max_ops,
        agent_call_budget=agent_call_budget,
    )
    return await interpreter.run(module)


class _Interpreter:
    """One script run. Not reusable — state is the run."""

    def __init__(
        self,
        *,
        source: str,
        host: ScriptHost,
        args: dict[str, Any],
        journal: JournalPort | None,
        max_ops: int,
        agent_call_budget: int,
    ) -> None:
        self._source = source
        self._args = args
        self._host = host
        self._journal = journal
        self._max_ops = max_ops
        self._agent_call_budget = agent_call_budget
        self._names: dict[str, Any] = {"args": args}
        self._ops = 0
        self._seq = 0
        self._agent_calls = 0
        self._logs = 0
        self._phases: list[str] = []
        self._replay_through = len(journal) if journal is not None else 0

    async def run(self, module: ast.Module) -> ScriptOutcome:
        """Execute the module and translate however it ended into an outcome."""
        try:
            await self._record_provenance()
            await self._exec_body(module.body)
        except _Complete as done:
            return self._outcome("completed", result=done.value)
        except _Pause as paused:
            return self._outcome("paused", kind=paused.kind, message=paused.message)
        except Cancelled as exc:
            return self._outcome("cancelled", error=str(exc))
        except BudgetExceeded as exc:
            return self._outcome("budget_exceeded", error=str(exc))
        except JournalError as exc:
            # The record no longer describes this script, so neither replaying
            # a recorded result nor re-running the effect is safe. Reporting the
            # run as failed hands that call to the operator instead of guessing.
            return self._outcome("failed", error=str(exc))
        except (ScriptError, HostError) as exc:
            return self._outcome("failed", error=str(exc))
        except Exception as exc:
            # Last resort, and deliberately after every typed clause so the
            # control-flow signals and terminal errors still win. Everything a
            # script touches is Python, and untrusted model output steers the
            # arguments: an unhandled TypeError here would kill the whole run
            # instead of degrading to a reported script failure.
            return self._outcome("failed", error=f"the script hit an unexpected error: {exc}")
        return self._outcome(
            "failed",
            error="the script ended without calling complete(...) or pause(...), "
            "so it produced no answer",
        )

    async def _record_provenance(self) -> None:
        """Bind the journal to the exact script text and input that produced it.

        Without this the journal records only *what was asked of the host*, so a
        run given a different task, or a different script over the same recorded
        calls, replays someone else's answers and reports them as its own.

        It is also the layer beneath the pinned script's signature. A pinned
        script is read from a workspace the agent can write, and the dry run
        checks *form, not intent* — a substituted script that is perfectly
        grammatical passes it. Seq 0 holding the hash of the source about to run
        is what turns that substitution into a refusal instead of an execution.

        The hash is taken over ``self._source`` — the same string
        :func:`execute_script` handed to :func:`parse_script`, at the moment it
        is about to run. Nothing normalises, reformats or re-serialises it in
        between, and nothing may start doing so: a hash over a tidied form would
        agree with a script that is not the one being executed.
        """
        payload = {
            "script": sha256(self._source.encode("utf-8")).hexdigest(),
            "args": self._args,
        }
        try:
            await self._host_call("_provenance", payload, _no_effect)
        except JournalDivergence as exc:
            # Say what it means. An operator reading the generic
            # "nondeterministic or edited mid-run" line has no way to tell this
            # apart from a flaky script, and here it may mean the pinned script
            # was replaced between the check and the run.
            raise ScriptError(
                "this journal belongs to a different run: the script text or the "
                "task it was given has changed since the journal was written. "
                "Nothing was replayed and nothing was executed. If this script "
                "was pinned, treat a change it did not make as tampering and "
                f"re-verify it before resuming. ({exc})"
            ) from exc

    def _outcome(self, status: Any, **fields: Any) -> ScriptOutcome:
        return ScriptOutcome(
            status=status,
            phases_seen=list(self._phases),
            agent_calls=self._agent_calls,
            **fields,
        )

    # --- statements ---------------------------------------------------------

    async def _exec_body(self, body: list[ast.stmt]) -> None:
        for statement in body:
            await self._exec(statement)

    async def _exec(self, node: ast.stmt) -> None:
        self._tick()
        if isinstance(node, ast.Expr):
            await self._eval(node.value)
        elif isinstance(node, ast.Assign):
            value = await self._eval(node.value)
            for target in node.targets:
                await self._assign(target, value)
        elif isinstance(node, ast.AugAssign):
            current = await self._eval(node.target)
            operand = await self._eval(node.value)
            await self._assign(node.target, self._binary(node.op, current, operand))
        elif isinstance(node, ast.For):
            await self._exec_for(node)
        elif isinstance(node, ast.While):
            await self._exec_while(node)
        elif isinstance(node, ast.If):
            branch = node.body if _truthy(await self._eval(node.test)) else node.orelse
            await self._exec_body(branch)
        elif isinstance(node, ast.Break):
            raise _Break
        elif isinstance(node, ast.Continue):
            raise _Continue

    async def _exec_for(self, node: ast.For) -> None:
        items = await self._eval(node.iter)
        for item in _as_sequence(items):
            self._tick()
            await self._assign(node.target, item)
            try:
                await self._exec_body(node.body)
            except _Break:
                return
            except _Continue:
                continue
        await self._exec_body(node.orelse)

    async def _exec_while(self, node: ast.While) -> None:
        while _truthy(await self._eval(node.test)):
            self._tick()
            try:
                await self._exec_body(node.body)
            except _Break:
                return
            except _Continue:
                continue
        await self._exec_body(node.orelse)

    async def _assign(self, target: ast.expr, value: Any) -> None:
        if isinstance(target, ast.Name):
            self._names[target.id] = value
        elif isinstance(target, (ast.Tuple, ast.List)):
            unpacked = _as_sequence(value)
            if len(unpacked) != len(target.elts):
                raise ScriptError(
                    f"cannot unpack {len(unpacked)} values into {len(target.elts)} names"
                )
            for element, item in zip(target.elts, unpacked, strict=True):
                await self._assign(element, item)
        elif isinstance(target, ast.Subscript):
            container = await self._eval(target.value)
            key = await self._eval(target.slice)
            self._set_item(container, key, value)

    def _set_item(self, container: Any, key: Any, value: Any) -> None:
        if isinstance(container, dict):
            _check_key(key)
            container[key] = value
            _guard(container)
        elif isinstance(container, list):
            if not isinstance(key, int) or isinstance(key, bool):
                raise ScriptError(f"a list index must be a whole number, not {key!r}")
            if not -len(container) <= key < len(container):
                raise ScriptError(f"index {key} is outside a list of {len(container)} items")
            container[key] = value
        else:
            raise ScriptError(f"cannot assign into a {_type_name(container)}")

    # --- expressions --------------------------------------------------------

    async def _eval(self, node: ast.expr) -> Any:
        self._tick()
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return self._read_name(node)
        if isinstance(node, ast.List):
            return _guard([await self._eval(item) for item in node.elts])
        if isinstance(node, ast.Tuple):
            return tuple([await self._eval(item) for item in node.elts])
        if isinstance(node, ast.Dict):
            return await self._eval_dict(node)
        if isinstance(node, ast.Subscript):
            return self._get_item(await self._eval(node.value), await self._eval(node.slice))
        if isinstance(node, ast.Slice):
            return await self._eval_slice(node)
        if isinstance(node, ast.Compare):
            return await self._eval_compare(node)
        if isinstance(node, ast.BoolOp):
            return await self._eval_boolop(node)
        if isinstance(node, ast.UnaryOp):
            return self._unary(node.op, await self._eval(node.operand))
        if isinstance(node, ast.BinOp):
            return self._binary(node.op, await self._eval(node.left), await self._eval(node.right))
        if isinstance(node, ast.IfExp):
            chosen = node.body if _truthy(await self._eval(node.test)) else node.orelse
            return await self._eval(chosen)
        if isinstance(node, ast.ListComp):
            return await self._eval_listcomp(node)
        if isinstance(node, ast.JoinedStr):
            return await self._eval_joined(node)
        if isinstance(node, ast.FormattedValue):
            return await self._eval_formatted(node)
        if isinstance(node, ast.Call):
            return await self._eval_call(node)
        raise ScriptError(f"{type(node).__name__} cannot be evaluated")

    def _read_name(self, node: ast.Name) -> Any:
        if node.id in self._names:
            return self._names[node.id]
        raise ScriptError(f"name {node.id!r} is used before it is set")

    async def _eval_dict(self, node: ast.Dict) -> dict[Any, Any]:
        result: dict[Any, Any] = {}
        for key_node, value_node in zip(node.keys, node.values, strict=True):
            if key_node is None:
                raise ScriptError("dictionary unpacking is not part of the script language")
            key = await self._eval(key_node)
            _check_key(key)
            result[key] = await self._eval(value_node)
        return _guard(result)

    async def _eval_slice(self, node: ast.Slice) -> slice:
        lower = await self._eval(node.lower) if node.lower is not None else None
        upper = await self._eval(node.upper) if node.upper is not None else None
        step = await self._eval(node.step) if node.step is not None else None
        return slice(lower, upper, step)

    async def _eval_compare(self, node: ast.Compare) -> bool:
        left = await self._eval(node.left)
        for operator, comparator_node in zip(node.ops, node.comparators, strict=True):
            right = await self._eval(comparator_node)
            if not _compare(operator, left, right):
                return False
            left = right
        return True

    async def _eval_boolop(self, node: ast.BoolOp) -> Any:
        """Short-circuits, so a guard like ``r and r["ok"]`` is safe to write."""
        result: Any = None
        for index, operand in enumerate(node.values):
            result = await self._eval(operand)
            if isinstance(node.op, ast.And) and not _truthy(result):
                return result
            if isinstance(node.op, ast.Or) and _truthy(result):
                return result
            if index == len(node.values) - 1:
                return result
        return result

    async def _eval_listcomp(self, node: ast.ListComp) -> list[Any]:
        saved = dict(self._names)
        try:
            return _guard(await self._comprehend(node, node.generators))
        finally:
            self._names = saved

    async def _comprehend(self, node: ast.ListComp, rest: list[ast.comprehension]) -> list[Any]:
        if not rest:
            return [await self._eval(node.elt)]
        generator, remaining = rest[0], rest[1:]
        collected: list[Any] = []
        for item in _as_sequence(await self._eval(generator.iter)):
            self._tick()
            await self._assign(generator.target, item)
            if not all([_truthy(await self._eval(test)) for test in generator.ifs]):
                continue
            collected.extend(await self._comprehend(node, remaining))
            _guard(collected)
        return collected

    async def _eval_joined(self, node: ast.JoinedStr) -> str:
        parts = [_render(await self._eval(value)) for value in node.values]
        _check_total_length(sum(len(part) for part in parts))
        return "".join(parts)

    async def _eval_formatted(self, node: ast.FormattedValue) -> str:
        """Renders one ``{...}`` hole.

        Values here are only the JSON-shaped types the language can build, so
        formatting them cannot reach a user-defined ``__format__``. The spec is
        still checked: its width and precision are multipliers on output length,
        and untrusted child output routinely lands in one.
        """
        value = await self._eval(node.value)
        if node.conversion == ord("r"):
            return _render(value, quoted=True)
        if node.format_spec is None:
            return _render(value)
        spec = str(await self._eval(node.format_spec))
        _check_format_spec(spec)
        try:
            return format(value, spec)
        except (TypeError, ValueError) as exc:
            raise ScriptError(f"{spec!r} is not a usable format for this value: {exc}") from exc

    # --- calls --------------------------------------------------------------

    async def _eval_call(self, node: ast.Call) -> Any:
        arguments = [await self._eval(argument) for argument in node.args]
        keywords = {str(keyword.arg): await self._eval(keyword.value) for keyword in node.keywords}
        if isinstance(node.func, ast.Attribute):
            receiver = await self._eval(node.func.value)
            return _call_method(receiver, node.func.attr, arguments, keywords)
        name = node.func.id if isinstance(node.func, ast.Name) else ""
        if name in _BUILTINS:
            return _call_builtin(name, arguments, keywords)
        return await self._call_host(name, arguments, keywords)

    async def _call_host(self, name: str, arguments: list[Any], keywords: dict[str, Any]) -> Any:
        if keywords:
            raise ScriptError(
                f"{name}() takes positional arguments only; pass options as a mapping, "
                f'for example agent("...", {{"label": "scout"}})'
            )
        if name == "complete":
            raise _Complete(arguments[0] if arguments else None)
        if name == "pause":
            return self._pause(arguments)
        if name == "phase":
            return self._phase(arguments)
        if name == "log":
            return self._log(arguments)
        if name == "agent":
            return await self._call_agent(arguments)
        if name == "parallel":
            return await self._call_parallel(arguments)
        if name == "budget":
            return await self._host_call("budget", None, self._do_budget)
        if name == "scratch_write":
            return await self._call_scratch_write(arguments)
        if name == "scratch_read":
            return await self._call_scratch_read(arguments)
        raise ScriptError(
            f"{name!r} is not a function; only the host functions and the builtins can be called"
        )

    def _pause(self, arguments: list[Any]) -> Any:
        kind = _string_argument(arguments, 0, "pause")
        if kind not in _PAUSE_KINDS:
            raise ScriptError(f"{kind!r} is not a pause kind; use one of {sorted(_PAUSE_KINDS)}")
        raise _Pause(kind, _string_argument(arguments, 1, "pause"))

    def _phase(self, arguments: list[Any]) -> None:
        title = _string_argument(arguments, 0, "phase")
        if len(self._phases) >= MAX_PHASES:
            raise ScriptError(
                f"the script announced more than {MAX_PHASES} phases; "
                f"a phase names a stage of the plan, not every step of a loop"
            )
        self._phases.append(title)
        if not self._is_replaying:
            self._host.phase(title)

    def _log(self, arguments: list[Any]) -> None:
        message = _string_argument(arguments, 0, "log")
        self._logs += 1
        if self._logs > MAX_HOST_CALLS:
            raise ScriptError(
                f"the script logged more than {MAX_HOST_CALLS} lines; "
                f"it is most likely looping without making progress"
            )
        if not self._is_replaying:
            self._host.log(message)

    async def _call_agent(self, arguments: list[Any]) -> Any:
        spec = _build_spec(_string_argument(arguments, 0, "agent"), _optional_mapping(arguments))
        self._reserve_agent_calls(1)

        async def perform() -> Any:
            return (await self._host.spawn(spec)).to_value()

        return await self._host_call("agent", _spec_payload(spec), perform)

    async def _call_parallel(self, arguments: list[Any]) -> Any:
        jobs = arguments[0] if arguments else None
        if not isinstance(jobs, list):
            raise ScriptError("parallel() takes a list of job mappings")
        if len(jobs) > MAX_PARALLEL:
            raise ScriptError(
                f"parallel() accepts at most {MAX_PARALLEL} jobs at once, got {len(jobs)}"
            )
        specs = [_build_spec(_job_prompt(job, index), job) for index, job in enumerate(jobs)]
        self._reserve_agent_calls(len(specs))

        async def perform() -> Any:
            return [outcome.to_value() for outcome in await self._host.spawn_many(specs)]

        return await self._host_call("parallel", [_spec_payload(s) for s in specs], perform)

    async def _call_scratch_write(self, arguments: list[Any]) -> Any:
        name = _string_argument(arguments, 0, "scratch_write")
        content = _string_argument(arguments, 1, "scratch_write")

        async def perform() -> Any:
            return self._host.scratch_write(name, content)

        return await self._host_call("scratch_write", [name, content], perform)

    async def _call_scratch_read(self, arguments: list[Any]) -> Any:
        name = _string_argument(arguments, 0, "scratch_read")

        async def perform() -> Any:
            return self._host.scratch_read(name)

        return await self._host_call("scratch_read", [name], perform)

    async def _do_budget(self) -> Any:
        state = self._host.budget()
        return state.to_value()

    # --- host boundary ------------------------------------------------------

    @property
    def _is_replaying(self) -> bool:
        """True while every host call so far has come from the journal."""
        return self._seq < self._replay_through

    def _reserve_agent_calls(self, count: int) -> None:
        """Charge the agent budget before anything is spawned, never after."""
        if self._agent_calls + count > self._agent_call_budget:
            raise AgentQuotaExceeded(
                f"this run may start {self._agent_call_budget} agents; "
                f"{self._agent_calls} are already spent and the script asked for {count} more"
            )
        self._agent_calls += count

    async def _host_call(self, kind: str, payload: Any, perform: Any) -> Any:
        """Perform an effect once, ever.

        On a resumed run the recorded result is returned and ``perform`` is not
        called. A terminal failure is deliberately not recorded: raising a
        budget ceiling and resuming must re-issue the call for real rather than
        replay a canned failure and fail identically forever.
        """
        seq = self._seq
        self._seq += 1
        digest = request_hash(kind, payload)
        if self._journal is not None:
            replayed, value = self._journal.replay(seq, kind, digest)
            if replayed:
                return value
        result = await perform()
        if self._journal is not None:
            self._journal.record(seq, kind, digest, result)
        return result

    def _tick(self) -> None:
        self._ops += 1
        if self._ops > self._max_ops:
            raise ScriptError(
                f"the script exceeded its operation budget of {self._max_ops}; "
                f"it is most likely looping without making progress"
            )

    # --- operators ----------------------------------------------------------

    def _binary(self, operator: ast.operator, left: Any, right: Any) -> Any:
        try:
            if isinstance(operator, ast.Add):
                _check_concatenation(left, right)
                return _guard(left + right)
            if isinstance(operator, ast.Sub):
                return left - right
            if isinstance(operator, ast.Mult):
                _check_repetition(left, right)
                return _guard(left * right)
            if isinstance(operator, ast.Div):
                return left / right
            if isinstance(operator, ast.FloorDiv):
                return left // right
            return left % right
        except ZeroDivisionError as exc:
            raise ScriptError("division by zero") from exc
        except OverflowError as exc:
            raise ScriptError(
                f"this arithmetic produced a number too large to hold: {exc}"
            ) from exc
        except TypeError as exc:
            raise ScriptError(
                f"cannot combine a {_type_name(left)} and a {_type_name(right)}: {exc}"
            ) from exc

    def _unary(self, operator: ast.unaryop, value: Any) -> Any:
        if isinstance(operator, ast.Not):
            return not _truthy(value)
        try:
            return -value if isinstance(operator, ast.USub) else +value
        except TypeError as exc:
            raise ScriptError(f"cannot negate a {_type_name(value)}") from exc

    def _get_item(self, container: Any, key: Any) -> Any:
        """Reads fail loudly. A silent ``None`` lets a broken plan look sound."""
        if isinstance(container, dict):
            _check_key(key)
            if key not in container:
                raise ScriptError(f"no key {key!r} in this mapping")
            return container[key]
        if isinstance(container, (list, tuple, str)):
            if isinstance(key, bool):
                # Python reads True as 1; writes already refuse it. Reading and
                # writing an index must agree on what an index is.
                raise ScriptError(
                    f"a {_type_name(container)} index must be a whole number, not {key!r}"
                )
            try:
                return container[key]
            except IndexError as exc:
                raise ScriptError(
                    f"index {key!r} is outside a {_type_name(container)} of {len(container)} items"
                ) from exc
            except TypeError as exc:
                raise ScriptError(f"cannot index a {_type_name(container)} with {key!r}") from exc
        raise ScriptError(f"cannot index a {_type_name(container)}")


# --- helpers ----------------------------------------------------------------


_ValueT = TypeVar("_ValueT")


def _truthy(value: Any) -> bool:
    return bool(value)


def _type_name(value: Any) -> str:
    """Plain-language type names, because the reader may be a language model."""
    return {
        "str": "text",
        "int": "whole number",
        "float": "number",
        "bool": "true/false",
        "list": "list",
        "dict": "mapping",
        "NoneType": "nothing",
    }.get(type(value).__name__, type(value).__name__)


def _guard(value: _ValueT) -> _ValueT:
    """Refuse a value that has grown past what a script has any business holding.

    A backstop only. It measures a value that already exists, so every path that
    can allocate a large one must bound its *inputs* first — see
    :func:`_check_repetition` and :func:`_check_serialisable`.
    """
    if isinstance(value, str) and len(value) > MAX_STRING_LENGTH:
        raise ScriptError(
            f"a text value grew to {len(value)} characters, past the "
            f"{MAX_STRING_LENGTH} limit — the result is too large"
        )
    if isinstance(value, (list, dict)) and len(value) > MAX_CONTAINER_LENGTH:
        raise ScriptError(
            f"a collection grew to {len(value)} items, past the "
            f"{MAX_CONTAINER_LENGTH} limit — the result is too large"
        )
    return value


def _check_key(key: Any) -> None:
    """A mapping key must be one of the language's own scalar values.

    A list as a key is a plain script mistake; letting Python's ``TypeError``
    out of the interpreter would make it look like a crash instead.
    """
    if not isinstance(key, (str, int, float, bool, type(None))):
        raise ScriptError(f"a {_type_name(key)} cannot be used as a mapping key")


async def _no_effect() -> None:
    """A host call that records a fact and does nothing."""
    return None


def _limit_for(value: Any) -> int:
    return MAX_STRING_LENGTH if isinstance(value, str) else MAX_CONTAINER_LENGTH


def _check_total_length(size: int, sample: Any = "") -> None:
    """Refuse a result by its computed size, before anything is allocated."""
    limit = _limit_for(sample)
    if size > limit:
        raise ScriptError(
            f"this would build {size} {'characters' if isinstance(sample, str) else 'items'}, "
            f"past the {limit} limit — refused before it was created"
        )


def _check_repetition(left: Any, right: Any) -> None:
    """Bound ``sequence * count`` by arithmetic rather than by the result.

    ``"a" * 400000000`` allocated 400 MB and *then* asked whether it was too
    large. Multiplying the two lengths costs nothing and refuses first.
    """
    for sequence, count in ((left, right), (right, left)):
        if isinstance(sequence, (str, list, tuple)) and isinstance(count, int):
            _check_total_length(len(sequence) * max(count, 0), sequence)
            return


def _check_concatenation(left: Any, right: Any) -> None:
    """Bound ``a + b`` the same way, so two legal halves cannot make one bomb."""
    if isinstance(left, (str, list, tuple)) and type(left) is type(right):
        _check_total_length(len(left) + len(right), left)


def _check_requested_length(size: int) -> None:
    """Bound a sequence a builtin is being asked to *materialise*."""
    if size > MAX_CONTAINER_LENGTH:
        raise ScriptError(
            f"this asks for {size} items, past the {MAX_CONTAINER_LENGTH} limit — "
            f"refused before the list was built"
        )


def _length_of(value: Any) -> int:
    """The item count of anything the language can iterate."""
    if isinstance(value, (str, list, tuple, dict, range)):
        return len(value)
    raise ScriptError(f"cannot count a {_type_name(value)}")


def _check_format_spec(spec: str) -> None:
    """Refuse a width or precision that would render more than a string may hold."""
    for number in _SPEC_NUMBERS.findall(spec):
        if int(number) > MAX_STRING_LENGTH:
            raise ScriptError(
                f"the format {spec!r} would render more than {MAX_STRING_LENGTH} "
                f"characters — refused before it was created"
            )


def _estimated_size(value: Any) -> int:
    """Serialised character count of a whole value, measured once per object.

    Aliasing is the hole a per-value length check cannot see: ten thousand
    references to one legal string compose into ten gigabytes, and every guard
    along the way reads a length of one. Measuring each distinct object once and
    multiplying by its occurrences gives the true size for the cost of the
    *distinct* structure, and makes a self-referential value a named error
    instead of a stack overflow.
    """
    sizes: dict[int, int] = {}
    on_path: set[int] = set()

    def measure(node: Any, depth: int) -> int:
        if isinstance(node, str):
            return len(node) + 2
        if isinstance(node, int) and not isinstance(node, bool):
            # Digit count without rendering it: a long integer is exactly the
            # value whose repr() is expensive and may itself refuse to run.
            return node.bit_length() // 3 + 3
        if not isinstance(node, (list, tuple, dict)):
            return len(repr(node))
        if depth > MAX_VALUE_DEPTH:
            raise ScriptError(
                f"this value nests deeper than {MAX_VALUE_DEPTH} levels, so it cannot be rendered"
            )
        key = id(node)
        cached = sizes.get(key)
        if cached is not None:
            return cached
        if key in on_path:
            raise ScriptError("this value contains itself, so it cannot be rendered")
        on_path.add(key)
        total = 2 + 2 * len(node)
        items = node.items() if isinstance(node, dict) else ((None, item) for item in node)
        for name, item in items:
            total += (0 if name is None else measure(name, depth + 1)) + measure(item, depth + 1)
        on_path.discard(key)
        sizes[key] = total
        return total

    return measure(value, 0)


def _check_serialisable(value: Any) -> None:
    """Refuse to render a value whose text form would be too large."""
    if isinstance(value, (list, tuple, dict)):
        _check_total_length(_estimated_size(value), "")


def _render(value: Any, *, quoted: bool = False) -> str:
    """Text form of a value, sized before it is built."""
    _check_serialisable(value)
    return repr(value) if quoted else str(value)


def _as_sequence(value: Any) -> list[Any]:
    """Everything iterable in this language is materialised, never streamed."""
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, range):
        return list(value)
    if isinstance(value, str):
        return list(value)
    if isinstance(value, dict):
        return list(value)
    raise ScriptError(f"cannot loop over a {_type_name(value)}")


def _compare(operator: ast.cmpop, left: Any, right: Any) -> bool:
    try:
        if isinstance(operator, ast.Eq):
            return bool(left == right)
        if isinstance(operator, ast.NotEq):
            return bool(left != right)
        if isinstance(operator, ast.In):
            return left in right
        if isinstance(operator, ast.NotIn):
            return left not in right
        if isinstance(operator, ast.Lt):
            return bool(left < right)
        if isinstance(operator, ast.LtE):
            return bool(left <= right)
        if isinstance(operator, ast.Gt):
            return bool(left > right)
        return bool(left >= right)
    except TypeError as exc:
        raise ScriptError(
            f"cannot compare a {_type_name(left)} with a {_type_name(right)}: {exc}"
        ) from exc


def _string_argument(arguments: list[Any], index: int, function: str) -> str:
    if index >= len(arguments):
        raise ScriptError(f"{function}() is missing an argument")
    value = arguments[index]
    if not isinstance(value, str):
        raise ScriptError(f"{function}() expects text, got a {_type_name(value)}")
    return value


def _optional_mapping(arguments: list[Any]) -> dict[str, Any]:
    if len(arguments) < 2:
        return {}
    options = arguments[1]
    if not isinstance(options, dict):
        raise ScriptError(f"agent() options must be a mapping, got a {_type_name(options)}")
    return options


def _job_prompt(job: Any, index: int) -> str:
    if not isinstance(job, dict):
        raise ScriptError(f"parallel() job {index} is a {_type_name(job)}, not a mapping")
    prompt = job.get("prompt")
    if not isinstance(prompt, str):
        raise ScriptError(f"parallel() job {index} has no 'prompt' text")
    return prompt


def _build_spec(prompt: str, options: dict[str, Any]) -> AgentSpec:
    """Turn script options into a spec, refusing anything unrecognised.

    A silently-ignored option is worse than an error: the script would look like
    it constrained a child that in fact ran unconstrained.
    """
    unknown = sorted(set(options) - _AGENT_OPTIONS)
    if unknown:
        raise ScriptError(
            f"unknown agent option(s) {', '.join(repr(key) for key in unknown)}; "
            f"the options are {sorted(_AGENT_OPTIONS)}"
        )
    mode = str(options.get("capability_mode", _DEFAULT_CAPABILITY_MODE))
    if mode not in _CAPABILITY_MODES:
        raise ScriptError(
            f"{mode!r} is not a capability mode; use one of {sorted(_CAPABILITY_MODES)}"
        )
    schema = options.get("output_schema")
    if schema is not None and not isinstance(schema, dict):
        raise ScriptError("'output_schema' must be a mapping")
    max_turns = options.get("max_turns")
    if max_turns is not None and (not isinstance(max_turns, int) or isinstance(max_turns, bool)):
        raise ScriptError("'max_turns' must be a whole number")
    return AgentSpec(
        prompt=prompt,
        label=str(options.get("label", "")),
        capability_mode=mode,
        output_schema=schema,
        max_turns=max_turns,
        phase=str(options.get("phase", "")),
    )


def _spec_payload(spec: AgentSpec) -> dict[str, Any]:
    """The hashed identity of a spawn request, for journal replay."""
    return {
        "prompt": spec.prompt,
        "label": spec.label,
        "capability_mode": spec.capability_mode,
        "output_schema": spec.output_schema,
        "max_turns": spec.max_turns,
        "phase": spec.phase,
    }


def _json_encode(value: Any) -> str:
    """Stable JSON, so a prompt built from data is byte-identical on replay."""
    _check_serialisable(value)
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ScriptError(f"cannot encode this value as JSON: {exc}") from exc


def _range(*bounds: Any) -> list[int]:
    """``range`` is the cheapest way to ask for gigabytes: it is lazy until it is
    materialised, and its own length is free to read first."""
    requested = range(*bounds)
    _check_requested_length(len(requested))
    return list(requested)


def _enumerate(value: Any, *start: Any) -> list[list[Any]]:
    _check_requested_length(_length_of(value))
    return [list(pair) for pair in enumerate(value, *start)]


def _zip(*values: Any) -> list[list[Any]]:
    _check_requested_length(min((_length_of(value) for value in values), default=0))
    return [list(pair) for pair in zip(*values, strict=False)]


def _reversed(value: Any) -> list[Any]:
    _check_requested_length(_length_of(value))
    return list(reversed(value))


def _sorted(value: Any, *, reverse: bool = False) -> list[Any]:
    _check_requested_length(_length_of(value))
    return sorted(value, reverse=reverse)


def _list(*value: Any) -> list[Any]:
    if value:
        _check_requested_length(_length_of(value[0]))
    return list(*value)


def _str(value: Any = "") -> str:
    return _render(value)


_BUILTINS: Final[dict[str, Any]] = {
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "dict": dict,
    "enumerate": _enumerate,
    "float": float,
    "int": int,
    "json_encode": _json_encode,
    "len": len,
    "list": _list,
    "max": max,
    "min": min,
    "range": _range,
    "reversed": _reversed,
    "round": round,
    "sorted": _sorted,
    "str": _str,
    "sum": sum,
    "zip": _zip,
}


def _call_builtin(name: str, arguments: list[Any], keywords: dict[str, Any]) -> Any:
    try:
        return _guard(_BUILTINS[name](*arguments, **keywords))
    except ScriptError:
        raise
    except (TypeError, ValueError, KeyError, IndexError, OverflowError) as exc:
        # Python names the bounded implementation; the script only knows the builtin.
        detail = str(exc).replace(f"_{name}(", f"{name}(")
        raise ScriptError(f"{name}() could not be used that way: {detail}") from exc


def _call_method(receiver: Any, name: str, arguments: list[Any], keywords: dict[str, Any]) -> Any:
    """Dispatch a container method by the receiver's real type.

    The type is checked first and the operation is written out here, so no
    attribute is ever looked up on a value the script produced. That is what
    keeps ``jobs.append(x)`` from being a way into an object graph.
    """
    if keywords:
        raise ScriptError(f"{name}() takes positional arguments only")
    try:
        if isinstance(receiver, list):
            return _list_method(receiver, name, arguments)
        if isinstance(receiver, dict):
            return _dict_method(receiver, name, arguments)
        if isinstance(receiver, str):
            return _str_method(receiver, name, arguments)
    except ScriptError:
        raise
    except (TypeError, ValueError, IndexError) as exc:
        raise ScriptError(f"{name}() could not be used that way: {exc}") from exc
    raise ScriptError(f"a {_type_name(receiver)} has no method {name!r}")


_LIST_METHODS: Final = frozenset({"append", "count", "extend", "index", "sort"})
_DICT_METHODS: Final = frozenset({"get", "items", "keys", "values"})
_STR_METHODS: Final = frozenset(
    {
        "count",
        "endswith",
        "index",
        "join",
        "lower",
        "replace",
        "split",
        "startswith",
        "strip",
        "upper",
    }
)
"""Dispatch is gated on these, so a name the grammar admits but no dispatcher
implements is impossible rather than a script that parses and then dies."""


def _list_method(receiver: list[Any], name: str, arguments: list[Any]) -> Any:
    if name not in _LIST_METHODS:
        raise ScriptError(f"a list has no method {name!r}")
    if name == "append":
        receiver.append(arguments[0])
        _guard(receiver)
        return None
    if name == "extend":
        receiver.extend(_as_sequence(arguments[0]))
        _guard(receiver)
        return None
    if name == "sort":
        receiver.sort()
        return None
    if name == "index":
        return receiver.index(arguments[0])
    return receiver.count(arguments[0])


def _dict_method(receiver: dict[Any, Any], name: str, arguments: list[Any]) -> Any:
    if name not in _DICT_METHODS:
        raise ScriptError(f"a mapping has no method {name!r}")
    if name == "get":
        return receiver.get(arguments[0], arguments[1] if len(arguments) > 1 else None)
    if name == "keys":
        return list(receiver.keys())
    if name == "values":
        return list(receiver.values())
    return [list(pair) for pair in receiver.items()]


def _str_method(receiver: str, name: str, arguments: list[Any]) -> Any:
    if name not in _STR_METHODS:
        raise ScriptError(f"text has no method {name!r}")
    if name == "upper":
        return receiver.upper()
    if name == "lower":
        return receiver.lower()
    if name == "strip":
        return receiver.strip()
    if name == "split":
        return receiver.split(*arguments) if arguments else receiver.split()
    if name == "join":
        return _join(receiver, arguments[0])
    if name == "startswith":
        return receiver.startswith(arguments[0])
    if name == "endswith":
        return receiver.endswith(arguments[0])
    if name == "replace":
        return _replace(receiver, arguments[0], arguments[1])
    if name == "index":
        return receiver.index(arguments[0])
    return receiver.count(arguments[0])


def _join(separator: str, parts: Any) -> str:
    """Size the whole join before building it.

    Ten thousand references to one legal one-megabyte string is a legal list of
    ten items' worth of guard checks and a ten-gigabyte result.
    """
    rendered = [_render(part) for part in _as_sequence(parts)]
    total = sum(len(part) for part in rendered) + len(separator) * max(0, len(rendered) - 1)
    _check_total_length(total)
    return separator.join(rendered)


def _replace(receiver: str, old: str, new: str) -> str:
    """Refuse a substitution that would multiply the string past its ceiling."""
    if isinstance(old, str) and isinstance(new, str) and old:
        _check_total_length(len(receiver) + receiver.count(old) * max(0, len(new) - len(old)))
    return receiver.replace(old, new)


__all__ = [
    "MAX_CONTAINER_LENGTH",
    "MAX_OPS",
    "MAX_PHASES",
    "MAX_STRING_LENGTH",
    "MAX_VALUE_DEPTH",
    "JournalPort",
    "ScriptError",
    "execute_script",
]
