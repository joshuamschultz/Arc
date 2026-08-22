"""`arc agent run` — one-shot non-interactive task execution."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path

import arcrun
from arcokf import OKFValidationError, validate

from arccli.commands.agent._common import (
    _load_arcagent,
    _load_env,
    _print_result_json,
    _resolve_agent_dir,
    _scaffold_workspace,
)


async def _collect_agent_stream(
    stream: AsyncIterator[arcrun.StreamEvent], *, emit_tokens: bool
) -> tuple[arcrun.RunResult, bool]:
    """Drain one agent stream while optionally rendering token events.

    The terminal event remains authoritative for totals and final status. The
    boolean tells terminal callers whether content was already rendered, so a
    CLI never prints a streamed answer twice.
    """
    token_text: list[str] = []
    terminal: arcrun.TurnEndEvent | None = None
    streamed = False
    async for event in stream:
        if isinstance(event, arcrun.TokenEvent):
            token_text.append(event.text)
            if emit_tokens and event.text:
                sys.stdout.write(event.text)
                sys.stdout.flush()
                streamed = True
        elif isinstance(event, arcrun.TurnEndEvent):
            terminal = event

    if terminal is None:
        return arcrun.RunResult(content="".join(token_text)), streamed
    return (
        arcrun.RunResult(
            content=terminal.final_text,
            turns=terminal.turns,
            tool_calls_made=terminal.tool_calls_made,
            cost_usd=terminal.cost_usd,
            tokens_used=terminal.tokens_used,
            completion_payload=terminal.completion_payload,
            completion_tool=terminal.completion_tool,
        ),
        streamed,
    )


def _default_session_id() -> str:
    """Dated rolling session id — one transcript per day, not one forever.

    A fixed ``cli:run`` id piled every task into a single ever-growing
    ``workspace/sessions/cli:run.jsonl`` (unbounded context bloat + cross-task
    bleed). Rolling on the date bounds each transcript to a day while still
    letting same-day invocations resume shared context. Pass ``--session`` to
    pin an explicit id.
    """
    return f"cli:run:{date.today().isoformat()}"


async def _agent_run_once(
    agent_dir: Path,
    task: str,
    model_override: str | None,
    context: str | None,
    verbose: bool,
    as_json: bool,
    session_id: str,
) -> None:
    """One-shot task execution coroutine."""
    arc_agent, config, _config_path = _load_arcagent(agent_dir)
    _scaffold_workspace(agent_dir, config.agent.name)

    if model_override:
        config.llm.model = model_override

    if context is not None:
        context_path = agent_dir / "workspace" / "context.md"
        context_path.parent.mkdir(parents=True, exist_ok=True)
        source = Path(context)
        if source.is_file():
            content = source.read_text(encoding="utf-8")
        else:
            content = context
        validation = validate(content, path=context_path.name)
        if not validation.valid:
            raise OKFValidationError(validation.diagnostics)
        context_path.write_text(content, encoding="utf-8")

    await arc_agent.startup()
    try:
        # One streaming entry — open-or-resume a local session and collect the
        # stream to a final result (SPEC-027 AC-2.2). Session id is dated/rolling
        # (or --session) so tasks don't all pile into one unbounded transcript.
        session = await arc_agent.session(session_id)
        result, streamed = await _collect_agent_stream(
            arc_agent.run(task, session=session), emit_tokens=not as_json
        )

        if as_json:
            _print_result_json(result)
        else:
            if streamed:
                sys.stdout.write("\n")
            elif result.content:
                sys.stdout.write(result.content + "\n")
            if verbose:
                sys.stdout.write(
                    f"\n[{result.turns} turns, {result.tool_calls_made} tool calls, "
                    f"${result.cost_usd:.4f}]\n"
                )
    finally:
        await arc_agent.shutdown()


def _run(args: argparse.Namespace) -> None:
    """Run a task against an agent (non-interactive one-shot)."""
    agent_dir = _resolve_agent_dir(args.path)
    _load_env(agent_dir)
    session_id = getattr(args, "session", None) or _default_session_id()
    asyncio.run(
        _agent_run_once(
            agent_dir=agent_dir,
            task=args.task,
            model_override=getattr(args, "model", None),
            context=getattr(args, "context", None),
            verbose=getattr(args, "verbose", False),
            as_json=getattr(args, "as_json", False),
            session_id=session_id,
        )
    )
