"""`arc agent extensions` — list capability files across scan roots."""

from __future__ import annotations

import argparse
import sys

from arccli.commands.agent._common import (
    _capability_scan_roots,
    _iter_capability_files,
    _print_table,
    _resolve_agent_dir,
)


def _extensions(args: argparse.Namespace) -> None:
    """List Python capability files across all four scan roots.

    `extensions` is preserved as an alias for backwards-compatible muscle
    memory; SPEC-021 calls these "capability files."
    """
    agent_dir = _resolve_agent_dir(args.path)

    rows: list[list[str]] = []
    for root_name, py_file in _iter_capability_files(agent_dir):
        rows.append([py_file.stem, root_name, str(py_file)])

    if rows:
        _print_table(["Name", "Source", "Path"], rows)
    else:
        sys.stdout.write("No capability files found.\n")
        for root_name, root in _capability_scan_roots(agent_dir):
            sys.stdout.write(f"  {root_name}: {root}\n")
