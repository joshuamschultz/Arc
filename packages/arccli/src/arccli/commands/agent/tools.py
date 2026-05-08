"""`arc agent tools` — list tools available to an agent."""

from __future__ import annotations

import argparse
import json
import sys

from arccli.commands.agent._common import _discover_tools, _resolve_agent_dir


def _tools(args: argparse.Namespace) -> None:
    """List all tools available to an agent."""
    agent_dir = _resolve_agent_dir(args.path)
    tools = _discover_tools(agent_dir)

    if getattr(args, "with_code_exec", False):
        from arcrun import make_execute_tool

        tools.append(make_execute_tool())

    if getattr(args, "json", False):
        data = [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
                "timeout_seconds": t.timeout_seconds,
            }
            for t in tools
        ]
        sys.stdout.write(json.dumps(data, indent=2) + "\n")
        return

    if not tools:
        sys.stdout.write("No tools found.\n")
        return
    for t in tools:
        sys.stdout.write(f"  {t.name}\n")
        sys.stdout.write(f"    {t.description}\n")
        params = t.input_schema.get("properties", {})
        required = t.input_schema.get("required", [])
        if params:
            for pname, pdef in params.items():
                req = " (required)" if pname in required else ""
                ptype = pdef.get("type", "?")
                pdesc = pdef.get("description", "")
                sys.stdout.write(f"    - {pname}: {ptype}{req} — {pdesc}\n")
        if t.timeout_seconds:
            sys.stdout.write(f"    timeout: {t.timeout_seconds}s\n")
        sys.stdout.write("\n")
