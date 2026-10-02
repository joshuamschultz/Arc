"""``arc knowledge`` — the connected-source lifecycle, headless (alpha-2 P26).

::

    arc knowledge sources   --agent A --email E
    arc knowledge status    [SOURCE] --agent A --email E
    arc knowledge resources SOURCE
    arc knowledge select    SOURCE RESOURCE_ID...
    arc knowledge map       SOURCE [--homes document,memory]
    arc knowledge approve   SOURCE
    arc knowledge sync|reindex SOURCE
    arc knowledge revoke    SOURCE [--yes]
    arc knowledge activate

Every verb is the same call the Knowledge tab makes. The CLI holds no business
logic and builds no offline service: the RUNNING agent owns the connected-data
service, so the CLI logs in to ArcUI's operator HTTP API (see
:mod:`arccli.commands._operator_http`) and the server audits each mutation.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Any

from arccli.commands._operator_http import (
    OperatorCall,
    agent_name,
    fail,
    operator_session,
    safe_segment,
    server_url,
)
from arccli.commands._shared import dispatch, print_json, print_table, write
from arccli.commands.skill_improve import add_remote_arguments, confirm

_PROG = "arc knowledge"
#: Source scans and syncs run third-party code; allow a slow provider.
_CALL_TIMEOUT_SECONDS = 120.0


def _prog(sub: str) -> str:
    return f"{_PROG} {sub}"


def _base(args: argparse.Namespace) -> tuple[str, str]:
    """(server URL, the agent's knowledge API base) — validated before any login."""
    prog = _prog(args.subcmd)
    if not args.agent or not args.email:
        fail(prog, "this runs on the running agent: pass --agent <name> and --email <operator>")
    url = server_url(prog, args.url)
    return url, f"/api/agents/{agent_name(prog, args.agent)}/knowledge"


def _source_path(args: argparse.Namespace, base: str) -> str:
    source = safe_segment(_prog(args.subcmd), args.source, "source id")
    return f"{base}/connected-sources/{source}"


def _with_call(args: argparse.Namespace, run: Callable[[OperatorCall, str], None]) -> None:
    url, base = _base(args)
    with operator_session(_prog(args.subcmd), url, args.email) as call:
        run(call, base)


def _show(args: argparse.Namespace, body: dict[str, Any], lines: Callable[[], None]) -> None:
    if args.json:
        print_json(body)
    else:
        lines()


def _items(body: dict[str, Any]) -> list[dict[str, Any]]:
    items = body.get("items")
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


# ---------------------------------------------------------------------------
# Read verbs
# ---------------------------------------------------------------------------


def _print_sources(items: list[dict[str, Any]]) -> None:
    if not items:
        write("No connected sources. Connect one with `arc connector add`, then grant it.")
        return
    print_table(
        ["SOURCE", "LABEL", "KIND", "STATUS", "ITEMS", "LAST SYNC", "ERROR"],
        [
            [
                str(i.get("connection_id", "")),
                str(i.get("label", "")),
                str(i.get("source_kind", "")),
                str(i.get("status", "")),
                str(i.get("documents_indexed", 0)),
                str(i.get("last_synced_at") or "never"),
                str(i.get("error_code") or "-"),
            ]
            for i in items
        ],
    )


def _sources(args: argparse.Namespace) -> None:
    def run(call: OperatorCall, base: str) -> None:
        body = call("GET", f"{base}/connected-sources", timeout=_CALL_TIMEOUT_SECONDS)
        _show(args, body, lambda: _print_sources(_items(body)))

    _with_call(args, run)


def _status(args: argparse.Namespace) -> None:
    def run(call: OperatorCall, base: str) -> None:
        body = call("GET", f"{base}/sync", timeout=_CALL_TIMEOUT_SECONDS)
        items = _items(body)
        if args.source:
            items = [
                i for i in items if args.source in (i.get("connection_id"), i.get("source_id"))
            ]
            if not items:
                fail(_prog("status"), f"source {args.source!r} not found")
            body = {"items": items}
        _show(args, body, lambda: _print_status(items))

    _with_call(args, run)


def _print_status(items: list[dict[str, Any]]) -> None:
    if not items:
        write("No connected sources.")
        return
    for item in items:
        write(f"{item.get('connection_id', '')}: {item.get('status', '')}")
        write(f"  last sync : {item.get('last_synced_at') or 'never'}")
        write(f"  items     : {item.get('documents_indexed', 0)}")
        write(f"  error     : {item.get('error_code') or 'none'}")
        if item.get("detail"):
            write(f"  detail    : {item['detail']}")


def _resources(args: argparse.Namespace) -> None:
    def run(call: OperatorCall, base: str) -> None:
        body = call("GET", f"{_source_path(args, base)}/resources", timeout=_CALL_TIMEOUT_SECONDS)
        _show(args, body, lambda: _print_resources(_items(body)))

    _with_call(args, run)


def _print_resources(items: list[dict[str, Any]]) -> None:
    if not items:
        write("This source offers nothing to select.")
        return
    print_table(
        ["RESOURCE", "LABEL", "KIND", "SELECTED"],
        [
            [
                str(i.get("resource_id", "")),
                str(i.get("label", "")),
                str(i.get("resource_kind", "")),
                "yes" if i.get("selected") else "no",
            ]
            for i in items
        ],
    )


# ---------------------------------------------------------------------------
# Write verbs
# ---------------------------------------------------------------------------


def _select(args: argparse.Namespace) -> None:
    if not args.resource_ids:
        fail(_prog("select"), "name at least one resource id (see `arc knowledge resources`)")

    def run(call: OperatorCall, base: str) -> None:
        body = call(
            "POST",
            f"{_source_path(args, base)}/resources",
            json={"resource_ids": list(args.resource_ids)},
            timeout=_CALL_TIMEOUT_SECONDS,
        )
        _show(args, body, lambda: _print_resources(_items(body)))

    _with_call(args, run)


def _map(args: argparse.Namespace) -> None:
    def run(call: OperatorCall, base: str) -> None:
        path = f"{_source_path(args, base)}/mapping"
        if args.homes:
            homes = [h.strip() for h in args.homes.split(",") if h.strip()]
            body = call("POST", path, json={"homes": homes}, timeout=_CALL_TIMEOUT_SECONDS)
        else:
            body = call("GET", path, timeout=_CALL_TIMEOUT_SECONDS)
        _show(args, body, lambda: _print_proposal(body.get("item")))

    _with_call(args, run)


def _print_proposal(item: object) -> None:
    if not isinstance(item, dict):
        write("No mapping staged. Stage one with `arc knowledge map SOURCE --homes ...`.")
        return
    write(f"mapping for {item.get('source_id', '')}: {item.get('status', '')}")
    write(f"  homes    : {', '.join(item.get('homes') or []) or '-'}")
    write(f"  allowed  : {', '.join(item.get('allowed_homes') or []) or '-'}")
    write(f"  approval : {item.get('approval_id') or '-'}")
    if item.get("detail"):
        write(f"  detail   : {item['detail']}")


def _approve(args: argparse.Namespace) -> None:
    def run(call: OperatorCall, base: str) -> None:
        proposal = call(
            "GET", f"{_source_path(args, base)}/mapping", timeout=_CALL_TIMEOUT_SECONDS
        ).get("item")
        approval_id = proposal.get("approval_id") if isinstance(proposal, dict) else None
        if not isinstance(approval_id, str) or not approval_id:
            fail(_prog("approve"), "no mapping is waiting for approval; run `arc knowledge map`")
        safe_segment(_prog("approve"), approval_id, "approval id")
        body = call("POST", f"/api/approvals/{approval_id}/approve", json={})
        _show(
            args, body, lambda: write(f"approved mapping {approval_id}: {body.get('status', '')}")
        )

    _with_call(args, run)


def _action(action: str) -> Callable[[argparse.Namespace], None]:
    def handler(args: argparse.Namespace) -> None:
        if action == "revoke" and not args.yes:
            if not confirm(f"Revoke {args.source}? Synced knowledge stops being served. [y/N] "):
                fail(_prog("revoke"), "not revoked")

        def run(call: OperatorCall, base: str) -> None:
            source = safe_segment(_prog(action), args.source, "source id")
            body = call(
                "POST", f"{base}/sync/{source}/{action}", json={}, timeout=_CALL_TIMEOUT_SECONDS
            )
            detail = f" ({body['detail']})" if body.get("detail") else ""
            _show(
                args, body, lambda: write(f"{source}: {action} {body.get('status', '')}{detail}")
            )

        _with_call(args, run)

    return handler


def _activate(args: argparse.Namespace) -> None:
    def run(call: OperatorCall, base: str) -> None:
        body = call(
            "POST", f"{base}/connected-data/activate", json={}, timeout=_CALL_TIMEOUT_SECONDS
        )
        _show(args, body, lambda: write(f"{body.get('status', '')}: {body.get('detail', '')}"))

    _with_call(args, run)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _verb(
    subs: Any, name: str, help_text: str, *, source: bool = True, yes: bool = False
) -> argparse.ArgumentParser:
    parser: argparse.ArgumentParser = subs.add_parser(name, help=help_text)
    if source:
        parser.add_argument("source", help="Source (connection) id.")
    add_remote_arguments(parser, required=False)
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    if yes:
        parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    return parser


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description=(
            "Connected-source knowledge lifecycle on a running agent — sources, status, "
            "resources, select, map, approve, sync, reindex, relayout, revoke, activate."
        ),
    )
    subs = parser.add_subparsers(dest="subcmd", metavar="<subcommand>")
    _verb(subs, "sources", "List every connected source and its sync state.", source=False)
    status = _verb(subs, "status", "Last sync, item count and errors.", source=False)
    status.add_argument("source", nargs="?", default=None, help="Limit to one source.")
    _verb(subs, "resources", "List what a source offers to index.")
    select = _verb(subs, "select", "Choose which resources to index.")
    select.add_argument("resource_ids", nargs="*", help="Resource ids to index.")
    mapping = _verb(subs, "map", "Show the mapping proposal, or stage one with --homes.")
    mapping.add_argument("--homes", default="", help="Comma-separated data homes to stage.")
    _verb(subs, "approve", "Approve the staged mapping with the operator key.")
    _verb(subs, "sync", "Sync a source now.")
    _verb(subs, "reindex", "Rebuild a source's index from scratch.")
    _verb(
        subs,
        "relayout",
        "Move a source's stored documents into folders that mirror the source (no re-embedding).",
    )
    _verb(subs, "revoke", "Stop serving a source's knowledge.", yes=True)
    _verb(subs, "activate", "Enable the connected-data module on the agent.", source=False)
    return parser


_SUBCOMMAND_MAP: dict[str, Callable[[argparse.Namespace], None]] = {
    "sources": _sources,
    "status": _status,
    "resources": _resources,
    "select": _select,
    "map": _map,
    "approve": _approve,
    "sync": _action("sync"),
    "reindex": _action("reindex"),
    "relayout": _action("relayout"),
    "revoke": _action("revoke"),
    "activate": _activate,
}


def knowledge_handler(args: list[str]) -> None:
    """Top-level handler for ``arc knowledge <sub> [args]``."""
    dispatch(_build_parser(), _SUBCOMMAND_MAP, args)


__all__ = ["knowledge_handler"]
