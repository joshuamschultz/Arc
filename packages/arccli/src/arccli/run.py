"""Low-level arcrun CLI — run tasks directly without an agent directory.

This exposes arcrun's full API surface for quick one-off tasks,
testing tools, and exploring features.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import click
from dotenv import load_dotenv

from arccli.formatting import click_echo, print_json, print_kv, print_table

_ENV_PATHS = [
    Path.home() / "AI" / "arcrun" / ".env",
    Path.home() / ".env",
    Path.cwd() / ".env",
]


def _load_env() -> None:
    for env_path in _ENV_PATHS:
        if env_path.exists():
            load_dotenv(env_path)


@click.group("run")
def run_group() -> None:
    """Run tasks directly with arcrun (no agent directory needed)."""


# ---------------------------------------------------------------------------
# task — one-shot task execution
# ---------------------------------------------------------------------------


@run_group.command("task")
@click.argument("prompt")
@click.option("--model", default="anthropic/claude-haiku-4-5-20251001", help="provider/model")
@click.option("--system", "system_prompt", default="You are a helpful assistant.", help="System prompt.")
@click.option("--max-turns", default=10, type=int, help="Max loop iterations.")
@click.option("--tool-timeout", default=None, type=float, help="Global tool timeout (seconds).")
@click.option("--strategy", default=None, type=click.Choice(["react", "code"]), help="Force strategy.")
@click.option("--with-code-exec", is_flag=True, help="Add execute_python tool.")
@click.option("--code-timeout", default=30, type=float, help="execute_python timeout.")
@click.option("--with-calc", is_flag=True, help="Add built-in calculator tool.")
@click.option("--verbose", is_flag=True, help="Show events inline.")
@click.option("--show-events", is_flag=True, help="Print full event log after run.")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def run_task(
    prompt: str,
    model: str,
    system_prompt: str,
    max_turns: int,
    tool_timeout: float | None,
    strategy: str | None,
    with_code_exec: bool,
    code_timeout: float,
    with_calc: bool,
    verbose: bool,
    show_events: bool,
    as_json: bool,
) -> None:
    """Run a single task with arcrun.

    \b
    Examples:
      arc run task "What is 2+2?" --with-calc
      arc run task "Write hello world" --with-code-exec --strategy code
      arc run task "Summarize this" --model openai/gpt-4o --verbose
      arc run task "Analyze data" --with-code-exec --show-events --json
    """
    _load_env()
    asyncio.run(_execute_task(
        prompt=prompt,
        model_id=model,
        system_prompt=system_prompt,
        max_turns=max_turns,
        tool_timeout=tool_timeout,
        strategy=strategy,
        with_code_exec=with_code_exec,
        code_timeout=code_timeout,
        with_calc=with_calc,
        verbose=verbose,
        show_events=show_events,
        as_json=as_json,
    ))


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
    verbose: bool,
    show_events: bool,
    as_json: bool,
) -> None:
    from arcllm import load_model
    from arcrun import Tool, ToolContext, make_execute_tool, run

    if "/" in model_id:
        provider, _, model_name = model_id.partition("/")
    else:
        provider, model_name = model_id, None

    llm = load_model(provider, model_name, telemetry=True)

    # Build tools
    tools: list[Tool] = []

    if with_calc:
        async def calculate(params: dict, ctx: ToolContext) -> str:
            expr = params["expression"]
            allowed = set("0123456789+-*/().% ")
            if not all(c in allowed for c in expr):
                return "Error: disallowed characters"
            try:
                return str(eval(expr))  # noqa: S307
            except Exception as e:
                return f"Error: {e}"

        tools.append(Tool(
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
        ))

    if with_code_exec:
        tools.append(make_execute_tool(timeout_seconds=code_timeout))

    if not tools:
        raise click.ClickException("No tools specified. Use --with-calc and/or --with-code-exec.")

    # Event logging
    from collections import Counter
    collected: list[Any] = []

    def event_handler(event: Any) -> None:
        if show_events:
            collected.append(event)
        if not verbose:
            return
        if event.type == "tool.start":
            click_echo(f"  [tool]   {event.data['name']}({event.data['arguments']})")
        elif event.type == "tool.end":
            click_echo(
                f"  [tool]   {event.data['name']} -> "
                f"{event.data['result_length']} chars ({event.data['duration_ms']:.0f}ms)"
            )
        elif event.type == "tool.denied":
            click_echo(f"  [denied] {event.data['name']}: {event.data['reason']}")
        elif event.type == "tool.error":
            click_echo(f"  [error]  {event.data['name']}: {event.data['error']}")
        elif event.type == "llm.call":
            click_echo(f"  [llm]    stop={event.data['stop_reason']}, latency={event.data['latency_ms']:.0f}ms")
        elif event.type == "turn.start":
            click_echo(f"  [turn]   --- turn {event.data['turn_number']} ---")

    allowed_strategies = [strategy] if strategy else None

    if verbose and not as_json:
        click_echo(f"Model: {model_id}")
        click_echo(f"Tools: {', '.join(t.name for t in tools)}")
        click_echo("-" * 50)

    result = await run(
        model=llm,
        tools=tools,
        system_prompt=system_prompt,
        task=prompt,
        max_turns=max_turns,
        allowed_strategies=allowed_strategies,
        on_event=event_handler,
        tool_timeout=tool_timeout,
    )

    if as_json:
        print_json({
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
        })
    else:
        if verbose:
            click_echo("-" * 50)
        if result.content:
            click_echo(result.content)
        if verbose:
            click_echo()
            click_echo(
                f"[{result.turns} turns, {result.tool_calls_made} tool calls, "
                f"${result.cost_usd:.4f}, strategy={result.strategy_used}]"
            )

    if show_events and not as_json:
        click_echo("\nEvent Log:")
        for i, event in enumerate(collected):
            data_str = str(event.data)
            if len(data_str) > 120:
                data_str = data_str[:120] + "..."
            click_echo(f"  {i + 1:3d}. [{event.type:25s}] {data_str}")

        click_echo()
        type_counts = Counter(e.type for e in collected)
        click_echo("Event Summary:")
        for t, c in sorted(type_counts.items()):
            click_echo(f"  {t:25s}: {c}")


# ---------------------------------------------------------------------------
# exec — direct Python code execution via make_execute_tool
# ---------------------------------------------------------------------------


@run_group.command("exec")
@click.argument("code")
@click.option("--timeout", default=30, type=float, help="Execution timeout (seconds).")
@click.option("--max-output", default=65536, type=int, help="Max output bytes.")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON result.")
def run_exec(code: str, timeout: float, max_output: int, as_json: bool) -> None:
    """Execute Python code directly via arcrun's sandboxed executor.

    \b
    Examples:
      arc run exec "print(2 + 2)"
      arc run exec "import math; print(math.pi)" --timeout 10
      arc run exec "print('hello')" --json
    """
    asyncio.run(_run_exec(code, timeout, max_output, as_json))


async def _run_exec(code: str, timeout: float, max_output: int, as_json: bool) -> None:
    import asyncio as aio

    from arcrun import ToolContext, make_execute_tool

    tool = make_execute_tool(timeout_seconds=timeout, max_output_bytes=max_output)
    ctx = ToolContext(
        run_id="cli-exec",
        tool_call_id="manual",
        turn_number=1,
        event_bus=None,
        cancelled=aio.Event(),
    )

    raw_result = await tool.execute({"code": code}, ctx)
    parsed = json.loads(raw_result)

    if as_json:
        print_json(parsed)
    else:
        if parsed.get("stdout"):
            click_echo(parsed["stdout"].rstrip())
        if parsed.get("stderr"):
            click_echo(f"stderr: {parsed['stderr'].rstrip()}")
        if parsed.get("exit_code", 0) != 0:
            click_echo(f"exit code: {parsed['exit_code']}")
        if parsed.get("duration_ms"):
            click_echo(f"({parsed['duration_ms']:.0f}ms)")


# ---------------------------------------------------------------------------
# version — show arcrun version and capabilities
# ---------------------------------------------------------------------------


@run_group.command("version")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def run_version(as_json: bool) -> None:
    """Show arcrun version and capabilities."""
    import arcllm
    import arcrun

    from arcrun.strategies import STRATEGIES, _load_strategies
    if not STRATEGIES:
        _load_strategies()

    data = {
        "arcrun": getattr(arcrun, "__version__", "0.1.0"),
        "arcllm": getattr(arcllm, "__version__", "0.1.0"),
        "strategies": list(STRATEGIES.keys()),
        "builtins": ["execute_python"],
        "public_api": [
            "run", "run_async", "RunHandle",
            "Tool", "ToolContext", "ToolRegistry",
            "LoopResult", "SandboxConfig",
            "Event", "EventBus", "Strategy",
            "make_execute_tool",
        ],
    }

    if as_json:
        print_json(data)
    else:
        print_kv([
            ("arcrun", data["arcrun"]),
            ("arcllm", data["arcllm"]),
            ("strategies", ", ".join(data["strategies"])),
            ("builtins", ", ".join(data["builtins"])),
        ])
        click_echo()
        click_echo("Public API:")
        for item in data["public_api"]:
            click_echo(f"  {item}")
