"""`arc agent config` — show agent configuration."""

from __future__ import annotations

import argparse
import json
import sys

from arccli.commands.agent._common import _load_agent_config, _resolve_agent_dir


def _config(args: argparse.Namespace) -> None:
    """Show agent configuration."""
    agent_dir = _resolve_agent_dir(args.path)
    config = _load_agent_config(agent_dir)

    if getattr(args, "json", False):
        sys.stdout.write(json.dumps(config, indent=2) + "\n")
        return

    for section, values in config.items():
        sys.stdout.write(f"[{section}]\n")
        if isinstance(values, dict):
            for key, val in values.items():
                sys.stdout.write(f"  {key} = {val}\n")
        else:
            sys.stdout.write(f"  {values}\n")
        sys.stdout.write("\n")
