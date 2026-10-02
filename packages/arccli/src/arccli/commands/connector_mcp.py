"""``arc connector add-mcp`` and ``arc connector sign`` — add your own MCP server.

The operator describes the server; Arc generates a connector bundle, signs it with the
operator key, installs it through the one shared path, and grants it. This module owns only
what that path cannot: reading the flags, asking for credentials with a hidden prompt (never
a flag, never an env file), showing the discovery preview, and taking the operator's choice of
tools. Every refusal (an unsafe command, a plain-http URL, a metadata address) is the
generator's, in :mod:`arcagent.modules.connectors.mcp_bundle`, so the CLI, the web dialog and
the TUI cannot disagree about what is safe.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, NoReturn

import arcagent
from pydantic import ValidationError

from arccli.commands._shared import print_table as _print_table
from arccli.commands._shared import write as _out

Fail = Callable[[str], NoReturn]


def add_mcp_arguments(parser: argparse.ArgumentParser) -> None:
    """The ``add-mcp`` flags. No ``--env-file``: credentials go through the secret path."""
    parser.add_argument("name", help="Name for this server, e.g. acme (lowercase, digits, _).")
    parser.add_argument("--http", default="", metavar="URL", help="A hosted server's https URL.")
    parser.add_argument(
        "--stdio",
        action="store_true",
        help="A local server. The command follows `--`, e.g. --stdio -- /usr/bin/server arg.",
    )
    parser.add_argument(
        "--command-arg", action="append", dest="command", default=[], help=argparse.SUPPRESS
    )
    parser.add_argument("--auth-header", default="", help="Header carrying the credential.")
    parser.add_argument("--auth-scheme", default="Bearer", help="Header scheme (default Bearer).")
    parser.add_argument(
        "--secret-env",
        action="append",
        default=[],
        metavar="FIELD=VAR",
        help="Pass a credential to a local server as environment variable VAR (prompted).",
    )
    parser.add_argument(
        "--allow",
        action="append",
        default=[],
        metavar="VERB[:read_only]",
        help="Expose this tool (repeatable). Default classification is state_modifying.",
    )
    parser.add_argument("--display", default="", help="Display name shown on the card.")
    parser.add_argument("--description", default="", help="One line about what it does.")
    parser.add_argument(
        "--preview", action="store_true", help="List the tools the server offers and stop."
    )
    parser.add_argument(
        "--agents", default="", help="Comma-separated agent names granted this server."
    )
    parser.add_argument("--extensions-root", default=None, help="Use exactly this bundle root.")
    parser.add_argument("--arc-dir", default=None, help="Arc config dir (default: ~/.arc).")
    parser.add_argument("--data-dir", default=None, help="Operational data dir for audit/state.")


def rewrite_command(args: Sequence[str]) -> list[str]:
    """Turn ``add-mcp NAME ... --stdio -- CMD ARG...`` into argparse-safe flags.

    argparse cannot take an open-ended positional after options without swallowing
    later flags, so everything after the first ``--`` becomes ``--command-arg=TOKEN``.
    The ``=`` form keeps a token that starts with ``-`` from being read as a flag.
    """
    if not args or args[0] != "add-mcp" or "--" not in args:
        return list(args)
    split = list(args).index("--")
    return [*args[:split], *(f"--command-arg={token}" for token in args[split + 1 :])]


def add_sign_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("bundle", help="Path to a bundle folder holding extension.toml.")
    parser.add_argument("--extensions-root", default=None, help="Use exactly this bundle root.")
    parser.add_argument("--arc-dir", default=None, help="Arc config dir (default: ~/.arc).")
    parser.add_argument("--data-dir", default=None, help="Operational data dir for audit/state.")


def add_mcp(
    args: argparse.Namespace,
    connections: arcagent.Connections,
    agents: Sequence[str],
    fail: Fail,
) -> None:
    """Discover, choose, generate, sign, install and grant."""
    spec = _spec_from(args, fail)
    secrets = _prompt_credentials(spec)
    try:
        found = asyncio.run(connections.preview_mcp_server(spec, secret_values=secrets))
    except arcagent.ExtensionError as exc:
        fail(exc.message)
    _print_table(
        ["tool", "usable", "description"],
        [[tool.name, "yes" if tool.usable else tool.reason, tool.description] for tool in found],
    )
    if args.preview:
        return
    chosen = _chosen_tools(args.allow, found, spec.transport, fail)
    try:
        added = asyncio.run(
            connections.add_mcp_server(
                spec.model_copy(update={"tools": chosen}), agents=agents, secret_values=secrets
            )
        )
    except arcagent.ExtensionError as exc:
        fail(exc.message)
    _out(f"Added MCP server '{added.report.instance}' (signed by {added.signed_by}).")
    _out(f"  spec sha256    : {added.spec_sha256}")
    _out(f"  granted to     : {', '.join(agents) or '(no agent yet — run: arc connector grant)'}")
    _out(f"  tools          : {', '.join(added.report.tools) or '(none served)'}")
    if added.report.detail:
        _out(f"  probe          : {added.report.detail}")
    if agents:
        _out("  Restart those agents for the server to attach.")


def sign(args: argparse.Namespace, connections: arcagent.Connections, fail: Fail) -> None:
    """Sign a hand-written bundle with the operator key, so it verifies at load."""
    try:
        signed = connections.sign_bundle(Path(args.bundle).expanduser())
    except arcagent.ExtensionError as exc:
        fail(exc.message)
    _out(f"Signed {len(signed)} file(s) in {args.bundle} with the operator key.")


def _spec_from(args: argparse.Namespace, fail: Fail) -> arcagent.McpServerSpec:
    if bool(args.http) == bool(args.stdio):
        fail("choose exactly one of --http URL or --stdio -- COMMAND ARGS")
    env_refs = _env_refs(args.secret_env, fail)
    try:
        if args.http:
            return arcagent.McpServerSpec(
                name=args.name,
                display=args.display,
                description=args.description,
                transport="http",
                url=args.http,
                auth_header=args.auth_header,
                auth_scheme=args.auth_scheme,
            )
        return arcagent.McpServerSpec(
            name=args.name,
            display=args.display,
            description=args.description,
            transport="stdio",
            argv=tuple(args.command),
            env_refs=env_refs,
        )
    except ValidationError as exc:
        fail("; ".join(str(error["msg"]) for error in exc.errors()))


def _env_refs(pairs: Sequence[str], fail: Fail) -> dict[str, str]:
    refs: dict[str, str] = {}
    for pair in pairs:
        field, _, variable = pair.partition("=")
        if not field or not variable:
            fail(f"--secret-env wants FIELD=VAR, not {pair!r}")
        refs[field] = variable
    return refs


def _prompt_credentials(spec: arcagent.McpServerSpec) -> dict[str, str]:
    """Hidden prompts only. A credential on argv would land in shell history."""
    return {
        field: getpass.getpass(f"Credential for {spec.name} [{field}] (hidden): ")
        for field in spec.secret_fields
    }


def _chosen_tools(
    allowed: Sequence[str],
    found: Sequence[arcagent.DiscoveredTool],
    transport: str,
    fail: Fail,
) -> dict[str, arcagent.McpToolChoice]:
    """The operator's tools, each classified by the operator and never by the server."""
    requested = _split(allowed) or _ask_which(found, fail)
    usable = {tool.name for tool in found if tool.usable}
    tags = arcagent.DEFAULT_HTTP_TAGS if transport == "http" else arcagent.DEFAULT_STDIO_TAGS
    chosen: dict[str, arcagent.McpToolChoice] = {}
    for item in requested:
        verb, _, classification = item.partition(":")
        if verb not in usable:
            fail(f"the server does not offer a usable tool named {verb!r}")
        if classification not in ("", "read_only", "state_modifying"):
            fail(f"{classification!r} is not a classification (read_only or state_modifying)")
        description = next((t.description for t in found if t.name == verb), "")
        chosen[verb] = arcagent.McpToolChoice(
            classification="read_only" if classification == "read_only" else "state_modifying",
            capability_tags=tags,
            description=description,
        )
    return chosen


def _split(allowed: Sequence[str]) -> list[str]:
    return [part.strip() for item in allowed for part in item.split(",") if part.strip()]


def _ask_which(found: Sequence[arcagent.DiscoveredTool], fail: Fail) -> list[str]:
    """Ask on a terminal; refuse to guess anywhere else."""
    if not sys.stdin.isatty():
        fail("choose the tools to expose with --allow VERB[:read_only] (repeat for each)")
    answer: Any = input("Expose which tools? (comma-separated names, or 'all'): ").strip()
    if answer == "all":
        return [tool.name for tool in found if tool.usable]
    return _split([answer])
