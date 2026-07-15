"""``arc trust`` — operator approval for gated agent capabilities (SPEC-021).

At enterprise/federal a self-executing capability (an agent-authored tool ``.py``
or a skill ``SKILL.md``) does not load until an operator pins its source hash
into the agent's ``[security.validators.approved]`` block. This command is that
on-box operator surface: it DISCOVERS gated capabilities via
``arcagent.capabilities.inventory`` (arcagent owns loading) and MUTATES the
approval store via ``arctrust`` (arctrust owns trust/approval).

``trust list [--agent <id>] [--all]``        — show gated capabilities.
``trust approve <name> [--agent <id>]``       — pin the current source hash.
``trust disapprove <name> [--agent <id>]``    — remove a pin (drift / revoke).

``--agent`` names an agent under the deployment's ``team/`` dir; it is optional
when the team has exactly one agent. The approver recorded is the on-box
deployment operator DID (``~/.arc/operator``) — the same key the agent's gate
pins to — so an approval is attributable and only an operator-key holder can mint
one.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

from arcagent.capabilities import artifact_signing
from arcagent.capabilities.inventory import (
    list_gated,
    pin_name_for,
    read_capability_source,
)
from arcgateway import team_roster
from arctrust import approve, disapprove

from arccli.commands._shared import dispatch
from arccli.commands._shared import print_table as _print_table
from arccli.commands._shared import write as _write


def _err(msg: str) -> None:
    sys.stderr.write(msg + "\n")


def _team_root() -> Path:
    """The deployment team dir — ``./team`` when present, else the cwd itself."""
    cwd_team = Path.cwd() / "team"
    return cwd_team if cwd_team.is_dir() else Path.cwd()


def _resolve_agent(agent_arg: str | None) -> tuple[str, Path, str]:
    """Resolve ``--agent`` to ``(agent_id, agent_root, label)`` under the team dir.

    Exits with a clear message when the team is empty, the named agent is
    unknown, or the flag is omitted while more than one agent exists.
    """
    team_root = _team_root()
    entries = team_roster.list_team(team_root=team_root, online_ids=set())
    if not entries:
        _err(f"arc trust: no agents found under {team_root}")
        sys.exit(1)
    if agent_arg:
        match = next((entry for entry in entries if entry.agent_id == agent_arg), None)
        if match is None:
            known = ", ".join(entry.agent_id for entry in entries)
            _err(f"arc trust: unknown agent {agent_arg!r}. Known agents: {known}")
            sys.exit(1)
        return match.agent_id, Path(match.workspace_path), match.display_name
    if len(entries) > 1:
        known = ", ".join(entry.agent_id for entry in entries)
        _err(f"arc trust: multiple agents ({known}); specify --agent <id>")
        sys.exit(1)
    only = entries[0]
    return only.agent_id, Path(only.workspace_path), only.display_name


def _operator_did() -> str:
    """The on-box deployment operator DID recorded as the approver."""
    from arctrust.policy import OperatorApprovalAuthority

    from arccli.commands.operator import resolve_operator_signer

    return OperatorApprovalAuthority(resolve_operator_signer()).did


def _now() -> str:
    """RFC3339 UTC timestamp for the approval record (injected into pure logic)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _list(args: argparse.Namespace) -> None:
    agent_id, agent_root, label = _resolve_agent(getattr(args, "agent", None))
    include_loaded = bool(getattr(args, "all", False))
    items = asyncio.run(
        list_gated(agent_root, agent_id=agent_id, agent_label=label, include_loaded=include_loaded)
    )
    if not items:
        _write("No capabilities found." if include_loaded else "No gated capabilities.")
        return
    rows = []
    for item in items:
        signed = "yes" if artifact_signing.sidecar_path(Path(item.path)).exists() else "no"
        short_hash = item.hash.split(":", 1)[-1][:12] if item.hash else "-"
        rows.append([item.name, item.kind, item.status, signed, short_hash, item.path])
    _print_table(["Name", "Kind", "Status", "Signed", "Hash", "Path"], rows)


def _approve(args: argparse.Namespace) -> None:
    agent_id, agent_root, label = _resolve_agent(getattr(args, "agent", None))
    config_path = agent_root / "arcagent.toml"
    gated = asyncio.run(list_gated(agent_root, agent_id=agent_id, agent_label=label))
    target = next((item for item in gated if item.name == args.name), None)
    if target is None:
        _err(f"arc trust: no gated capability named {args.name!r} for {agent_id}")
        sys.exit(1)
    source = read_capability_source(Path(target.path))
    if source is None:
        _err(f"arc trust: cannot read capability source at {target.path}")
        sys.exit(1)
    approver = _operator_did()
    approve(
        config_path,
        name=pin_name_for(target),
        source=source,
        approver=approver,
        timestamp=_now(),
    )

    # Re-scan through the inventory seam to report the post-approval verdict.
    after = asyncio.run(
        list_gated(agent_root, agent_id=agent_id, agent_label=label, include_loaded=True)
    )
    resolved = next((item for item in after if item.name == args.name), None)
    status = resolved.status if resolved is not None else "unknown"
    _write(f"Approved {args.name} on {agent_id} — status now: {status} (approver {approver}).")
    if status != "loaded":
        _write(
            "Note: this agent's tier does not consult pinned hashes at load "
            "(TofuLayer checks pins only at enterprise/federal). The approval is "
            "recorded but does not change what loads at personal tier."
        )


def _disapprove(args: argparse.Namespace) -> None:
    agent_id, agent_root, label = _resolve_agent(getattr(args, "agent", None))
    config_path = agent_root / "arcagent.toml"
    # Resolve the loader's pin name from the current inventory when the artifact
    # is still present; else treat the given name as the pin name directly (so a
    # pin for a since-deleted artifact can still be cleared).
    inventory = asyncio.run(
        list_gated(agent_root, agent_id=agent_id, agent_label=label, include_loaded=True)
    )
    target = next((item for item in inventory if item.name == args.name), None)
    pin_name = pin_name_for(target) if target is not None else args.name
    if disapprove(config_path, name=pin_name):
        _write(f"Removed approval for {args.name} on {agent_id}.")
    else:
        _write(f"No approval was pinned for {args.name} on {agent_id}.")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arc trust",
        description="Operator approval for gated agent capabilities — list, approve, disapprove.",
        add_help=True,
    )
    subs = parser.add_subparsers(dest="subcmd", metavar="<subcommand>")

    p_list = subs.add_parser("list", help="List gated (non-loaded) capabilities.")
    p_list.add_argument("--agent", dest="agent", default=None, help="Agent id under team/.")
    p_list.add_argument(
        "--all", dest="all", action="store_true", help="Include loaded capabilities too."
    )

    p_approve = subs.add_parser("approve", help="Pin a gated capability's current source hash.")
    p_approve.add_argument("name", help="Capability name (as shown by `trust list`).")
    p_approve.add_argument("--agent", dest="agent", default=None, help="Agent id under team/.")

    p_disapprove = subs.add_parser("disapprove", help="Remove a capability's approval pin.")
    p_disapprove.add_argument("name", help="Capability name to un-pin.")
    p_disapprove.add_argument("--agent", dest="agent", default=None, help="Agent id under team/.")

    return parser


_SUBCOMMAND_MAP = {
    "list": _list,
    "approve": _approve,
    "disapprove": _disapprove,
}


def trust_handler(args: list[str]) -> None:
    """Top-level handler for `arc trust <sub> [args]`."""
    dispatch(_build_parser(), _SUBCOMMAND_MAP, args)


__all__ = ["trust_handler"]
