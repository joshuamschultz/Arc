"""`arc agent tools` — list tools available to an agent."""

from __future__ import annotations

import argparse
import json
import sys

from arccli.commands.agent._common import (
    _discover_tools,
    _load_agent_config,
    _resolve_agent_dir,
)


def _agent_isolation(agent_dir: object) -> tuple[str, str | None]:
    """Resolve (tier, relax) for the agent's code-exec isolation floor.

    Tier is the agent's ``[security] tier`` (personal by default); relax is the
    explicit ``[execution] relax_isolation`` opt-down (unset by default). Both
    are forwarded to arcrun's router — arccli is only the seam that carries them.
    """
    from pathlib import Path

    cfg = _load_agent_config(Path(str(agent_dir)))
    tier = str(cfg.get("security", {}).get("tier", "personal"))
    relax_raw = cfg.get("execution", {}).get("relax_isolation")
    relax = str(relax_raw) if relax_raw else None
    return tier, relax


def _tools(args: argparse.Namespace) -> None:
    """List all tools available to an agent."""
    agent_dir = _resolve_agent_dir(args.path)
    tools = _discover_tools(agent_dir)

    if getattr(args, "with_code_exec", False):
        from pathlib import Path

        from arcrun import make_execute_tool

        tier, relax = _agent_isolation(agent_dir)
        # Attribute the backend-selection event to the agent's DID. This is a
        # read-only listing, so no live audit sink is wired (logger-only); a
        # persisted audit record belongs to an execution, not a `tools` listing.
        cfg = _load_agent_config(Path(str(agent_dir)))
        caller_did = cfg.get("identity", {}).get("did") or None
        tools.append(make_execute_tool(tier=tier, relax=relax, caller_did=caller_did))

    if getattr(args, "json", False):
        data = [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
                "timeout_seconds": getattr(t, "timeout_seconds", None),
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
        timeout = getattr(t, "timeout_seconds", None)
        if timeout:
            sys.stdout.write(f"    timeout: {timeout}s\n")
        sys.stdout.write("\n")
