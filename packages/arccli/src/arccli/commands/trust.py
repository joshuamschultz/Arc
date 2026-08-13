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

``list`` shows what is GATED (``--all`` for everything), but ``approve`` reaches
any discoverable capability, gated or already loading. That asymmetry is the
point. Signing is a statement about bytes, not a repair for a refusal: on a
personal-tier box a hand-written skill already loads and is never gated, so a
gated-only approve made it unsignable on the machine it was written on — which
is exactly the promote-a-laptop-skill-to-a-hardened-box workflow this exists for.
It also blocks pre-signing before shipping to a stricter tier, and re-signing an
artifact that loads today but was edited a minute ago. Re-signing is never a
no-op: it signs what is on disk NOW and re-pins that hash.

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
import sys
from pathlib import Path

import arcagent
from arcgateway import team_roster
from arctrust import Signer, SignerError, disapprove

from arccli.commands._shared import audit_chain, dispatch
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


def _operator_actor() -> str:
    """The DID a trust change is attributed to on the audit chain."""
    from arccli.commands.operator import resolve_operator_signer

    return _operator_did(resolve_operator_signer())


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


def _resolve_target(agent_id: str, agent_root: Path, label: str, name: str) -> arcagent.GatedItem:
    """Find the capability ``name`` names, gated or already loading.

    Resolved from the FULL inventory, never the gated-only listing. Restricting
    approval to what is currently gated made the product's headline workflow
    impossible: on a personal-tier box a hand-written skill already loads, so it
    is never gated — and so could never be signed on the laptop it was written
    on. The same restriction blocked pre-signing an artifact before shipping it
    to a stricter tier, and re-signing one that loads today but was just edited.

    Signing is an operator statement about bytes, not a repair for a refusal.
    """
    inventory = asyncio.run(
        arcagent.list_gated(agent_root, agent_id=agent_id, agent_label=label, include_loaded=True)
    )
    matches = [item for item in inventory if item.name == name]
    if not matches:
        _err(
            f"arc trust: no capability named {name!r} for {agent_id}. "
            f"Run `arc trust list --all --agent {agent_id}` to see every capability."
        )
        sys.exit(1)
    if len({item.path for item in matches}) > 1:
        # One name, two artifacts — a capability defined at two scan roots, where
        # precedence decides which one loads. Signing "the first one" would sign
        # a file the operator did not mean, so make them say which.
        paths = "\n  ".join(sorted(item.path for item in matches))
        _err(
            f"arc trust: {name!r} is ambiguous on {agent_id} — it names several "
            f"artifacts:\n  {paths}"
        )
        sys.exit(1)
    return matches[0]


def _approve(args: argparse.Namespace) -> None:
    agent_id, agent_root, label = _resolve_agent(getattr(args, "agent", None))
    config_path = agent_root / "arcagent.toml"
    target = _resolve_target(agent_id, agent_root, label, args.name)
    artifact = Path(target.path)
    # Read BEFORE signing: afterwards every artifact has a sidecar, so this is
    # the only moment that can tell a first signature from a re-signature.
    resigned = arcagent.sidecar_path(artifact).exists()
    signer = _operator_signer()
    approver = _operator_did(signer)
    try:
        with audit_chain("arc trust", _operator_actor) as (sink, _):
            arcagent.sign_capability(
                artifact,
                signer_did=approver,
                signer=signer,
                config_path=config_path,
                audit_sink=sink,
            )
    except (OSError, ValueError) as exc:
        _err(f"arc trust: could not sign {target.path}: {exc}")
        sys.exit(1)

    # Re-scan through the inventory seam to report the post-approval verdict.
    # Matched by PATH, not by name: a refused candidate is reported under its
    # file/folder name and a LOADED one under the name its metadata declares, so
    # a successful approval is precisely the case where the name changes. Looking
    # it up by name told the operator "still gated" every time it had worked.
    after = asyncio.run(
        arcagent.list_gated(agent_root, agent_id=agent_id, agent_label=label, include_loaded=True)
    )
    resolved = next((item for item in after if item.path == target.path), None)
    status = resolved.status if resolved is not None else "unknown"
    action = "Re-signed" if resigned else "Signed"
    over = "over its current bytes" if resigned else "over its bytes"
    _write(
        f"{action} {args.name} on {agent_id} {over} — signature written, key pinned, "
        f"hash pinned; status now: {status} (approver {approver})."
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
        with audit_chain("arc trust", _operator_actor) as (sink, actor):
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

    p_approve = subs.add_parser(
        "approve", help="Sign (or re-sign) any capability with the operator key."
    )
    p_approve.add_argument("name", help="Capability name (as shown by `trust list --all`).")
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
