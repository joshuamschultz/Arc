"""``arc trust`` — operator approval for gated agent capabilities (SPEC-021, SPEC-066).

A self-executing capability (an agent-authored tool ``.py`` or a skill
``SKILL.md``) does not load until an operator SIGNS it. Passing the gate needs
three things together — a detached signature over the artifact bytes, the
signer's key pinned as a trusted capability-verification key, and the source
hash pinned — so this command performs them as one action via
``arcagent.sign_capability``. A hash pin on its own never reaches the
enterprise/federal signature floor, which is why approval is signing.

``trust list [--agent <id>] [--all]``        — show gated capabilities.
``trust approve <name> [--agent <id>]``      — sign, pin the key, pin the hash.
``trust disapprove <name> [--agent <id>]``   — the exact inverse (drift / revoke).

``--agent`` names an agent under the deployment's ``team/`` dir; it is optional
when the team has exactly one agent. The signer is always the on-box deployment
operator key — there is no flag to supply an identity, so only an operator-key
holder can mint an approval. Custody is whatever the machine ``[security]``
block selects: an in-process seed under ``~/.arc/operator``, or a vault/notary
key that signs by reference and never enters this process. This is a CLI-only
surface: it is registered on no tool registry and reachable from no chat socket,
so no model can approve its own code (REQ-321).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from collections.abc import Iterator
from pathlib import Path

import arcagent
from arcgateway import team_roster
from arctrust import AuditSink, Signer, SignerError, disapprove

from arccli.commands._shared import dispatch
from arccli.commands._shared import print_table as _print_table
from arccli.commands._shared import write as _write

#: Actor recorded when the operator signer cannot be resolved. Only ever paired
#: with a discarding sink, so it names the degraded case rather than a person.
_UNRESOLVED_OPERATOR_DID = "did:arc:operator:unresolved"


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


def _operator_did(signer: Signer) -> str:
    """The on-box deployment operator DID recorded as the signer/approver."""
    from arctrust.policy import OperatorApprovalAuthority

    return OperatorApprovalAuthority(signer).did


def _operator_signer() -> Signer:
    """The deployment operator signer, or exit with what to do about it.

    Custody is config-selected and this command does not care which one it gets:
    ``in_process`` signs with the on-disk operator key, ``vault_transit`` signs
    by reference through the notary/HSM without the seed ever entering this
    process. An unresolvable transit is fail-closed by construction (NFR-3) —
    reaching for whatever key happens to be on disk would pin a trust anchor the
    deployment deliberately moved to a vault.
    """
    from arccli.commands.operator import resolve_operator_signer

    try:
        return resolve_operator_signer()
    except SignerError as exc:
        _err(f"arc trust: cannot resolve the operator signer — {exc}")
        sys.exit(1)


@contextlib.contextmanager
def _audit_chain() -> Iterator[tuple[AuditSink, str]]:
    """Open the deployment's operator-signed WORM chain for one trust change.

    Yields the sink the capability event is recorded on and the operator DID it
    is attributed to, both resolved from the one on-box operator key so the
    record and its actor cannot disagree. Opened per command and closed on exit:
    a ``WormSink`` holds an exclusive ``flock`` for its lifetime, so one left
    open locks every later writer out.

    An unresolvable signer or an unopenable chain degrades to a discarding sink
    and a warning instead of stopping the command — an operator must always be
    able to grant or withdraw trust, and auditing never interrupts the action it
    audits (NIST AU-5).
    """
    from arcstore import resolve_data_dir
    from arctrust import NullSink

    from arccli.commands.operator import operator_worm_sink, resolve_operator_signer

    try:
        actor = _operator_did(resolve_operator_signer())
        sink = operator_worm_sink(None, resolve_data_dir(None))
    except (OSError, RuntimeError, ValueError) as exc:
        _err(f"arc trust: audit chain unavailable ({type(exc).__name__}); change not recorded")
        yield NullSink(), _UNRESOLVED_OPERATOR_DID
        return
    try:
        yield sink, actor
    finally:
        sink.close()


def _list(args: argparse.Namespace) -> None:
    agent_id, agent_root, label = _resolve_agent(getattr(args, "agent", None))
    include_loaded = bool(getattr(args, "all", False))
    items = asyncio.run(
        arcagent.list_gated(
            agent_root, agent_id=agent_id, agent_label=label, include_loaded=include_loaded
        )
    )
    if not items:
        _write("No capabilities found." if include_loaded else "No gated capabilities.")
        return
    rows = []
    for item in items:
        signed = "yes" if arcagent.sidecar_path(Path(item.path)).exists() else "no"
        short_hash = item.hash.split(":", 1)[-1][:12] if item.hash else "-"
        rows.append([item.name, item.kind, item.status, signed, short_hash, item.path])
    _print_table(["Name", "Kind", "Status", "Signed", "Hash", "Path"], rows)


def _approve(args: argparse.Namespace) -> None:
    agent_id, agent_root, label = _resolve_agent(getattr(args, "agent", None))
    config_path = agent_root / "arcagent.toml"
    gated = asyncio.run(arcagent.list_gated(agent_root, agent_id=agent_id, agent_label=label))
    target = next((item for item in gated if item.name == args.name), None)
    if target is None:
        _err(f"arc trust: no gated capability named {args.name!r} for {agent_id}")
        sys.exit(1)
    signer = _operator_signer()
    approver = _operator_did(signer)
    try:
        with _audit_chain() as (sink, _):
            arcagent.sign_capability(
                Path(target.path),
                signer_did=approver,
                signer=signer,
                config_path=config_path,
                audit_sink=sink,
            )
    except (OSError, ValueError) as exc:
        _err(f"arc trust: could not sign {target.path}: {exc}")
        sys.exit(1)

    # Re-scan through the inventory seam to report the post-approval verdict.
    after = asyncio.run(
        arcagent.list_gated(agent_root, agent_id=agent_id, agent_label=label, include_loaded=True)
    )
    resolved = next((item for item in after if item.name == args.name), None)
    status = resolved.status if resolved is not None else "unknown"
    _write(
        f"Approved {args.name} on {agent_id} — signed, key pinned, hash pinned; "
        f"status now: {status} (approver {approver})."
    )
    if status != "loaded":
        detail = resolved.detail if resolved is not None else "no longer visible to the loader"
        _write(f"Still gated: {detail}")


def _disapprove(args: argparse.Namespace) -> None:
    agent_id, agent_root, label = _resolve_agent(getattr(args, "agent", None))
    config_path = agent_root / "arcagent.toml"
    inventory = asyncio.run(
        arcagent.list_gated(agent_root, agent_id=agent_id, agent_label=label, include_loaded=True)
    )
    target = next((item for item in inventory if item.name == args.name), None)
    if target is not None:
        with _audit_chain() as (sink, actor):
            arcagent.revoke_capability(
                Path(target.path),
                config_path=config_path,
                operator_did=actor,
                audit_sink=sink,
            )
        _write(
            f"Revoked {args.name} on {agent_id} — signature, trusted key, and hash pin removed."
        )
        return
    # The artifact is gone from disk but its pins are not: deleting a capability
    # file never touched the agent's config. Refusing here would strand a hash
    # pin AND a trusted verify key in that config with no command able to remove
    # them — a trust anchor outliving the code it was minted for. Clear what is
    # still reachable by name; the key cannot be resolved without the artifact's
    # signature sidecar, so say so rather than imply a full revocation.
    if disapprove(config_path, name=args.name):
        _write(
            f"Removed the hash pin for {args.name} on {agent_id} (artifact already deleted). "
            "Any trusted key it pinned could not be resolved from a missing artifact — "
            f"check [security.validators] trusted_keys in {config_path} if it is now unused."
        )
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

    p_approve = subs.add_parser("approve", help="Sign a gated capability with the operator key.")
    p_approve.add_argument("name", help="Capability name (as shown by `trust list`).")
    p_approve.add_argument("--agent", dest="agent", default=None, help="Agent id under team/.")

    p_disapprove = subs.add_parser("disapprove", help="Withdraw a capability's signature + pins.")
    p_disapprove.add_argument("name", help="Capability name to revoke.")
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
