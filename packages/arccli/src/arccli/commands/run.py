"""Plain CommandDef handlers for the `arc run` subcommand group."""

from __future__ import annotations

import argparse
import ast
import asyncio
import json
import operator
import os
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from arccli.commands._shared import dispatch
from arccli.commands._shared import print_json as _print_json
from arccli.commands._shared import print_kv as _print_kv
from arccli.commands._shared import write as _write

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Safe arithmetic for the `--with-calc` demo tool. Pow is deliberately excluded:
# the expression source is the LLM, and `9**9**9**9` is an unbounded-compute DoS
# (LLM10). The advertised surface is only + - * / ( ) %.
_ARITH_OPS: dict[type[ast.AST], Callable[..., float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _eval_arith(node: ast.AST) -> float:
    """Evaluate a whitelisted arithmetic AST node; reject everything else."""
    if isinstance(node, ast.Expression):
        return _eval_arith(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp):
        op_fn = _ARITH_OPS.get(type(node.op))
        if op_fn is None:
            raise ValueError(f"Unsupported operator: {type(node.op).__name__}")
        return op_fn(_eval_arith(node.left), _eval_arith(node.right))
    if isinstance(node, ast.UnaryOp):
        op_fn = _ARITH_OPS.get(type(node.op))
        if op_fn is None:
            raise ValueError(f"Unsupported operator: {type(node.op).__name__}")
        return op_fn(_eval_arith(node.operand))
    raise ValueError("Unsupported expression")


_ENV_PATHS = [
    Path.cwd() / ".env",
    Path.home() / ".arc" / ".env",
    Path.home() / ".env",
]

_MACHINE_CONFIG = Path.home() / ".arc" / "arcagent.toml"
_DIRECT_RUN_AUDIT = Path.home() / ".arc" / "audit" / "direct-run.jsonl"


def _machine_config() -> dict[str, Any]:
    """Load the machine-wide ``~/.arc/arcagent.toml``, or {} when absent/unreadable."""
    if not _MACHINE_CONFIG.exists():
        return {}
    try:
        with open(_MACHINE_CONFIG, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _machine_isolation() -> tuple[str, str | None]:
    """Resolve ``(tier, relax)`` for direct ``arc run`` code execution.

    Tier comes from ``ARC_TIER`` or the machine-wide ``~/.arc/arcagent.toml``
    ``[security].tier`` and defaults to personal ONLY when the host is genuinely
    unconfigured — so a federal/enterprise host runs sandboxed + audited, never as
    a bare host subprocess. relax comes from ``ARC_RELAX_ISOLATION`` or
    ``[execution].relax_isolation``; a genuinely-unconfigured personal box keeps
    ``arc run``'s documented dev-tool default of host execution (``"local"``),
    while any configured tier gets its real isolation floor (relax stays unset).
    """
    cfg = _machine_config()
    tier = (
        os.environ.get("ARC_TIER") or str(cfg.get("security", {}).get("tier", "")) or "personal"
    ).lower()
    relax_raw = os.environ.get("ARC_RELAX_ISOLATION") or cfg.get("execution", {}).get(
        "relax_isolation"
    )
    relax = str(relax_raw) if relax_raw else None
    if relax is None and tier == "personal":
        relax = "local"
    return tier, relax


def _worm_sink(identity: Any) -> Any | None:
    """Build the operator-signed tamper-evident WORM audit sink (SPEC-053).

    The chain is signed by the deployment OPERATOR key (the audit authority),
    never the caller's DID seed — the audited subject must not be its own audit
    authority. ``identity`` still gates whether a direct run is attributed at
    all. Best-effort (AU-5): audit degrades to disabled, never breaks the run.
    """
    if not identity.can_sign:
        return None
    try:
        from arctrust import WormSink

        from arccli.commands.operator import resolve_operator_signer

        _DIRECT_RUN_AUDIT.parent.mkdir(parents=True, exist_ok=True)
        # Config-resolved operator signer (custody + algorithm) — never a bare
        # Ed25519 default (SPEC-037 F3).
        return WormSink(_DIRECT_RUN_AUDIT, resolve_operator_signer())
    except Exception:  # reason: audit is best-effort — never break the run (AU-5)
        return None


def _direct_run_identity() -> tuple[str | None, Any | None]:
    """Return ``(caller_did, audit_sink)`` for a direct CLI run.

    Loads the standalone signing authority so ``execute_python``'s selection /
    downgrade / refuse events are attributed to the operator DID and PERSISTED to
    their WORM audit chain. Absent an authority (or if the chain cannot be opened)
    both degrade to None — logger-only, unattributed — never a hard failure.
    """
    from arccli.commands.identity import load_signing_authority

    identity = load_signing_authority()
    if identity is None:
        return None, None
    return identity.did, _worm_sink(identity)


def _load_env() -> None:
    """Load .env files without importing click."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    for env_path in _ENV_PATHS:
        if env_path.exists():
            load_dotenv(env_path)


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------


def _version(args: argparse.Namespace) -> None:
    """Show arcrun version and capabilities."""
    import arcllm
    import arcrun
    from arcrun.strategies import STRATEGIES, _load_strategies

    if not STRATEGIES:
        _load_strategies()

    as_json: bool = getattr(args, "as_json", False)
    data: dict[str, Any] = {
        "arcrun": getattr(arcrun, "__version__", "0.1.0"),
        "arcllm": getattr(arcllm, "__version__", "0.1.0"),
        "strategies": list(STRATEGIES.keys()),
        "builtins": ["execute_python"],
        "public_api": [
            "run",
            "run_async",
            "RunHandle",
            "Tool",
            "ToolContext",
            "ToolRegistry",
            "LoopResult",
            "SandboxConfig",
            "Event",
            "EventBus",
            "Strategy",
            "make_execute_tool",
        ],
    }

    if as_json:
        _print_json(data)
    else:
        _print_kv(
            [
                ("arcrun", data["arcrun"]),
                ("arcllm", data["arcllm"]),
                ("strategies", ", ".join(data["strategies"])),
                ("builtins", ", ".join(data["builtins"])),
            ]
        )
        _write()
        _write("Public API:")
        for item in data["public_api"]:
            _write(f"  {item}")


def _exec_cmd(args: argparse.Namespace) -> None:
    """Execute Python code directly via arcrun's sandboxed executor."""
    code: str = args.code
    timeout: float = getattr(args, "timeout", 30.0)
    max_output: int = getattr(args, "max_output", 65536)
    as_json: bool = getattr(args, "as_json", False)

    tier, relax = _machine_isolation()
    caller_did, audit_sink = _direct_run_identity()
    asyncio.run(
        _run_exec_async(code, timeout, max_output, as_json, tier, relax, caller_did, audit_sink)
    )


async def _run_exec_async(
    code: str,
    timeout: float,
    max_output: int,
    as_json: bool,
    tier: str,
    relax: str | None,
    caller_did: str | None,
    audit_sink: Any | None,
) -> None:
    from arcrun import ToolContext, make_execute_tool

    # Isolation is sourced from the machine config (never hardcoded): a personal
    # dev box runs sandbox-off on the host, an enterprise/federal host is routed
    # to its container/VM floor. Selection/downgrade/refuse events are attributed
    # to the operator DID and persisted to the audit sink.
    tool = make_execute_tool(
        timeout_seconds=timeout,
        max_output_bytes=max_output,
        tier=tier,
        relax=relax,
        caller_did=caller_did,
        audit_sink=audit_sink,
    )
    ctx = ToolContext(
        run_id="cli-exec",
        tool_call_id="manual",
        turn_number=1,
        event_bus=None,
        cancelled=asyncio.Event(),
    )

    raw_result = await tool.execute({"code": code}, ctx)
    parsed = json.loads(raw_result)

    if as_json:
        _print_json(parsed)
    else:
        if parsed.get("stdout"):
            _write(parsed["stdout"].rstrip())
        if parsed.get("stderr"):
            _write(f"stderr: {parsed['stderr'].rstrip()}")
        if parsed.get("exit_code", 0) != 0:
            _write(f"exit code: {parsed['exit_code']}")
        if parsed.get("duration_ms"):
            _write(f"({parsed['duration_ms']:.0f}ms)")


def _task(args: argparse.Namespace) -> None:
    """Run a single task with arcrun."""
    _load_env()

    prompt: str = args.prompt
    model: str = getattr(args, "model", "anthropic/claude-haiku-4-5-20251001")
    system_prompt: str = getattr(args, "system_prompt", "You are a helpful assistant.")
    max_turns: int = getattr(args, "max_turns", 10)
    tool_timeout: float | None = getattr(args, "tool_timeout", None)
    strategy: str | None = getattr(args, "strategy", None)
    with_code_exec: bool = getattr(args, "with_code_exec", False)
    code_timeout: float = getattr(args, "code_timeout", 30.0)
    with_calc: bool = getattr(args, "with_calc", False)
    no_spawn: bool = getattr(args, "no_spawn", False)
    spawn_token_budget: int | None = getattr(args, "spawn_token_budget", None)
    verbose: bool = getattr(args, "verbose", False)
    show_events: bool = getattr(args, "show_events", False)
    as_json: bool = getattr(args, "as_json", False)

    asyncio.run(
        _execute_task(
            prompt=prompt,
            model_id=model,
            system_prompt=system_prompt,
            max_turns=max_turns,
            tool_timeout=tool_timeout,
            strategy=strategy,
            with_code_exec=with_code_exec,
            code_timeout=code_timeout,
            with_calc=with_calc,
            with_spawn=not no_spawn,
            spawn_token_budget=spawn_token_budget,
            verbose=verbose,
            show_events=show_events,
            as_json=as_json,
        )
    )


async def _execute_task(
    *,
    prompt: str,
    model_id: str,
    system_prompt: str,
    max_turns: int,
    tool_timeout: float | None,
    strategy: str | None,
    with_code_exec: bool,
    code_timeout: float,
    with_calc: bool,
    with_spawn: bool,
    spawn_token_budget: int | None,
    verbose: bool,
    show_events: bool,
    as_json: bool,
) -> None:
    from arcagent.orchestration import RootTokenBudget, make_spawn_tool
    from arcllm import load_model
    from arcrun import StaticProvider, Tool, ToolContext, make_execute_tool, run

    from arccli.commands.identity import load_signing_authority

    if "/" in model_id:
        provider, _, model_name = model_id.partition("/")
    else:
        provider, model_name = model_id, None

    # Attribute this direct (no-agent) run to the standalone signing authority so
    # its LLM calls + tool events carry an identity in the audit trail. Run
    # `arc identity init` to create one. Non-fatal here (attribution, not a gate).
    authority = load_signing_authority()
    actor_did = authority.did if authority is not None else None
    audit_sink = _worm_sink(authority) if authority is not None else None
    if actor_did is None and not as_json:
        _write(
            "Note: no signing authority — run `arc identity init` to attribute/audit direct runs."
        )

    llm = load_model(
        provider,
        model_name,
        telemetry={"agent_did": actor_did} if actor_did else True,
        agent_label="arc-cli",
    )

    tools: list[Tool] = []

    if with_calc:

        async def calculate(params: dict[str, Any], ctx: ToolContext) -> str:
            expr = params["expression"]
            try:
                return str(_eval_arith(ast.parse(expr, mode="eval")))
            except (ValueError, SyntaxError, TypeError, ZeroDivisionError) as e:
                return f"Error: {e}"

        tools.append(
            Tool(
                name="calculate",
                description="Evaluate a math expression. Supports +, -, *, /, (), %.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "expression": {"type": "string", "description": "Math expression"},
                    },
                    "required": ["expression"],
                },
                execute=calculate,
            )
        )

    if with_code_exec:
        # Isolation is sourced from the machine config (never hardcoded); the
        # selection is attributed to the operator DID and persisted to the sink.
        tier, relax = _machine_isolation()
        tools.append(
            make_execute_tool(
                timeout_seconds=code_timeout,
                tier=tier,
                relax=relax,
                caller_did=actor_did,
                audit_sink=audit_sink,
            )
        )

    # Register spawn_task by default — agent decides which capabilities to expose.
    # The CLI plays the role of a thin agent here. Closure mutation lets nested
    # children inherit spawn_task too.
    if with_spawn:
        spawn_tool = make_spawn_tool(
            model=llm,
            tools=tools,
            system_prompt=system_prompt,
            root_token_budget=(
                RootTokenBudget(spawn_token_budget) if spawn_token_budget else None
            ),
        )
        tools.append(spawn_tool)

    if not tools:
        sys.stderr.write(
            "Error: No tools available. Pass --with-calc, --with-code-exec, or "
            "remove --no-spawn.\n"
        )
        sys.exit(1)

    from collections import Counter

    collected: list[Any] = []

    def event_handler(event: Any) -> None:
        if show_events:
            collected.append(event)
        if not verbose:
            return
        if event.type == "tool.start":
            _write(f"  [tool]   {event.data['name']}({event.data['arguments']})")
        elif event.type == "tool.end":
            _write(
                f"  [tool]   {event.data['name']} -> "
                f"{event.data['result_length']} chars ({event.data['duration_ms']:.0f}ms)"
            )
        elif event.type == "tool.denied":
            _write(f"  [denied] {event.data['name']}: {event.data['reason']}")
        elif event.type == "tool.error":
            _write(f"  [error]  {event.data['name']}: {event.data['error']}")
        elif event.type == "llm.call":
            stop = event.data["stop_reason"]
            latency = event.data["latency_ms"]
            _write(f"  [llm]    stop={stop}, latency={latency:.0f}ms")
        elif event.type == "turn.start":
            _write(f"  [turn]   --- turn {event.data['turn_number']} ---")

    allowed_strategies = [strategy] if strategy else None

    if verbose and not as_json:
        _write(f"Model: {model_id}")
        _write(f"Tools: {', '.join(t.name for t in tools)}")
        _write("-" * 50)

    result = await run(
        model=llm,
        capabilities=StaticProvider(tools),
        system_prompt=system_prompt,
        task=prompt,
        max_turns=max_turns,
        allowed_strategies=allowed_strategies,
        on_event=event_handler,
        tool_timeout=tool_timeout,
        actor_did=actor_did,
    )

    if as_json:
        _print_json(
            {
                "content": result.content,
                "turns": result.turns,
                "tool_calls_made": result.tool_calls_made,
                "tokens_used": result.tokens_used,
                "strategy_used": result.strategy_used,
                "cost_usd": result.cost_usd,
                "event_count": len(result.events),
                "events": [
                    {"type": e.type, "timestamp": e.timestamp, "data": e.data}
                    for e in result.events
                ],
            }
        )
    else:
        if verbose:
            _write("-" * 50)
        if result.content:
            _write(result.content)
        if verbose:
            _write()
            _write(
                f"[{result.turns} turns, {result.tool_calls_made} tool calls, "
                f"${result.cost_usd:.4f}, strategy={result.strategy_used}]"
            )

    if show_events and not as_json:
        _write("\nEvent Log:")
        for i, event in enumerate(collected):
            data_str = str(event.data)
            if len(data_str) > 120:
                data_str = data_str[:120] + "..."
            _write(f"  {i + 1:3d}. [{event.type:25s}] {data_str}")

        _write()
        type_counts = Counter(e.type for e in collected)
        _write("Event Summary:")
        for t, c in sorted(type_counts.items()):
            _write(f"  {t:25s}: {c}")


# ---------------------------------------------------------------------------
# Argparse-based dispatcher
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for `arc run <sub> [args]`."""
    parser = argparse.ArgumentParser(
        prog="arc run",
        description="Run tasks directly with arcrun (no agent directory needed).",
        add_help=True,
    )
    subs = parser.add_subparsers(dest="subcmd", metavar="<subcommand>")

    # version
    p = subs.add_parser("version", help="Show arcrun version and capabilities.")
    p.add_argument("--json", dest="as_json", action="store_true", help="Output as JSON.")

    # exec
    p = subs.add_parser("exec", help="Execute Python code via arcrun's sandboxed executor.")
    p.add_argument("code", help="Python code to execute.")
    p.add_argument("--timeout", type=float, default=30.0, help="Execution timeout (seconds).")
    p.add_argument("--max-output", dest="max_output", type=int, default=65536)
    p.add_argument("--json", dest="as_json", action="store_true", help="Output raw JSON result.")

    # task
    p = subs.add_parser("task", help="Run a single task with arcrun.")
    p.add_argument("prompt", help="Task prompt.")
    p.add_argument("--model", default="anthropic/claude-haiku-4-5-20251001", help="provider/model")
    p.add_argument(
        "--system",
        dest="system_prompt",
        default="You are a helpful assistant.",
        help="System prompt.",
    )
    p.add_argument("--max-turns", dest="max_turns", type=int, default=10)
    p.add_argument("--tool-timeout", dest="tool_timeout", type=float, default=None)
    p.add_argument("--strategy", choices=["react", "code"], default=None, help="Force strategy.")
    p.add_argument("--with-code-exec", dest="with_code_exec", action="store_true")
    p.add_argument("--code-timeout", dest="code_timeout", type=float, default=30.0)
    p.add_argument("--with-calc", dest="with_calc", action="store_true")
    p.add_argument(
        "--no-spawn",
        dest="no_spawn",
        action="store_true",
        help="Disable spawn_task tool (default: registered for parallel sub-task fan-out).",
    )
    p.add_argument(
        "--spawn-token-budget",
        dest="spawn_token_budget",
        type=int,
        default=None,
        help="Shared token pool (LLM10) across all spawned children; omit to leave uncapped.",
    )
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--show-events", dest="show_events", action="store_true")
    p.add_argument("--json", dest="as_json", action="store_true", help="Output as JSON.")

    return parser


_SUBCOMMAND_MAP = {
    "version": _version,
    "exec": _exec_cmd,
    "task": _task,
}


def run_handler(args: list[str]) -> None:
    """Top-level handler for `arc run <sub> [args]`.

    Called by arccli.commands.registry when the user runs `arc run ...`.
    """
    dispatch(_build_parser(), _SUBCOMMAND_MAP, args)
