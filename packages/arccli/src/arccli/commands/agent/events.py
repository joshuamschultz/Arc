"""`arc agent events` — list all event types emitted by arcrun and arcagent."""

from __future__ import annotations

import argparse

from arccli.commands.agent._common import _print_table


def _events(_args: argparse.Namespace) -> None:
    """List all event types emitted by arcrun and arcagent."""
    events = [
        ("loop.start", "run() called", "task, tool_names, strategy"),
        ("loop.complete", "Execution finished", "content, turns, tool_calls, tokens, cost"),
        ("loop.max_turns", "Hit turn limit", "turns_used, max_turns"),
        ("strategy.selected", "Strategy chosen", "strategy"),
        ("turn.start", "Loop iteration begins", "turn_number"),
        ("turn.end", "Loop iteration ends", "turn_number"),
        (
            "llm.call",
            "model.invoke() returned",
            "model, stop_reason, tokens, latency_ms, cost_usd",
        ),
        ("tool.start", "Tool execution begins", "name, arguments"),
        ("tool.end", "Tool execution complete", "name, result_length, duration_ms"),
        ("tool.denied", "Sandbox blocked tool", "name, reason"),
        ("tool.error", "Tool threw exception/timeout", "name, error"),
        ("tool.registered", "New tool added to registry", "name"),
        ("tool.replaced", "Existing tool replaced", "name"),
        ("tool.removed", "Tool removed from registry", "name"),
        ("agent:init", "ArcAgent startup complete", "agent_name, did"),
        ("agent:shutdown", "ArcAgent shutdown", ""),
        ("agent:pre_respond", "Before arcrun.run()", "task"),
        ("agent:post_respond", "After arcrun.run()", "content, turns"),
        ("agent:pre_tool", "Before tool execution", "name"),
        ("agent:post_tool", "After tool execution", "name, result_length"),
        ("agent:extensions_loaded", "Extensions discovered", "count"),
        ("agent:skills_loaded", "Skills discovered", "count"),
    ]
    _print_table(["Event", "When", "Data Keys"], [[e, w, d] for e, w, d in events])
