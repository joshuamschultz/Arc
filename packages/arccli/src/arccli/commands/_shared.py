"""Shared output, argparse-dispatch, and audit-chain helpers for arccli handlers.

Every ``arc <group>`` handler prints through the same primitives, ends with the
same subcommand-dispatch tail, and — where it changes what a deployment trusts —
records through the same operator-signed chain. Homing them here removes the
copy-pasted drift surface (one error-message format, one output path, one
degrade-to-NullSink decision).
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from collections.abc import Callable, Iterator, Mapping
from typing import TYPE_CHECKING

from arccli.formatting import print_json, print_kv, print_table

if TYPE_CHECKING:
    from arctrust import AuditSink

__all__ = [
    "UNRESOLVED_OPERATOR_DID",
    "audit_chain",
    "dispatch",
    "err",
    "print_json",
    "print_kv",
    "print_table",
    "write",
]

#: Actor recorded when the on-box operator identity cannot be resolved. Only
#: ever paired with a discarding sink, so it names the degraded case rather
#: than a person.
UNRESOLVED_OPERATOR_DID = "did:arc:operator:unresolved"


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


@contextlib.contextmanager
def audit_chain(prog: str, resolve_actor: Callable[[], str]) -> Iterator[tuple[AuditSink, str]]:
    """Open the deployment's operator-signed WORM chain for one operator command.

    Yields the sink the command's events are recorded on and the operator DID
    they are attributed to, both resolved from the one on-box operator key so
    the record and its actor cannot disagree. *prog* prefixes the degrade
    warning (e.g. ``"arc module"``); *resolve_actor* returns the DID and may
    raise if the operator identity is unreachable.

    Opened per command and closed on exit: a ``WormSink`` holds an exclusive
    ``flock`` for its lifetime, so one left open locks every later writer out.
    Held open for the whole command so a multi-bundle install lands as one
    contiguous run of records.

    An unresolvable operator or an unopenable chain degrades to a discarding
    sink and a warning instead of stopping the command — an operator must
    always be able to change what a deployment trusts, and auditing never
    interrupts the action it audits (NIST AU-5).
    """
    from arcstore import resolve_data_dir
    from arctrust import NullSink

    from arccli.commands.operator import operator_worm_sink

    try:
        actor = resolve_actor()
        sink = operator_worm_sink(None, resolve_data_dir(None))
    except (OSError, RuntimeError, ValueError) as exc:
        err(f"{prog}: audit chain unavailable ({type(exc).__name__}); change not recorded")
        yield NullSink(), UNRESOLVED_OPERATOR_DID
        return
    try:
        yield sink, actor
    finally:
        sink.close()
