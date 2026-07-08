"""Shared output + argparse-dispatch helpers for arccli command handlers.

Every ``arc <group>`` handler prints through the same primitives and ends with
the same subcommand-dispatch tail. Homing them here removes the copy-pasted
drift surface (one error-message format, one output path).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Mapping

from arccli.formatting import print_json, print_kv, print_table

__all__ = ["dispatch", "err", "print_json", "print_kv", "print_table", "write"]


def write(msg: str = "") -> None:
    """Write a line to stdout."""
    sys.stdout.write(msg + "\n")


def err(msg: str = "") -> None:
    """Write a line to stderr."""
    sys.stderr.write(msg + "\n")


def dispatch(
    parser: argparse.ArgumentParser,
    submap: Mapping[str, Callable[[argparse.Namespace], None]],
    args: list[str],
) -> None:
    """Parse *args* with *parser* and route to the matching subcommand.

    The shared tail for ``arc <group> <sub> [args]``. Prints help on an empty
    invocation or a bare group, and errors on an unknown subcommand. The group
    name in the error message is taken from ``parser.prog`` (e.g. ``"arc ext"``).
    """
    if not args:
        parser.print_help()
        sys.exit(0)

    parsed = parser.parse_args(args)

    if parsed.subcmd is None:
        parser.print_help()
        sys.exit(0)

    fn = submap.get(parsed.subcmd)
    if fn is None:
        sys.stderr.write(f"{parser.prog}: unknown subcommand '{parsed.subcmd}'\n")
        sys.exit(1)

    fn(parsed)
