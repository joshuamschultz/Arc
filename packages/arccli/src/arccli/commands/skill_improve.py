"""`arc skill improve` and the running-agent golden-set controls (alpha-2 P8).

::

    arc skill improve <skill> --agent <dir-or-name> --email EMAIL [--dry-run] [--yes] [--json]
    arc skill evals run <skill> --agent <dir-or-name> --email EMAIL [--json]
    arc skill evals regen <skill_path> --agent <dir-or-name> --email EMAIL [--yes]

The work runs on the RUNNING agent — it owns the eval model, the sandbox, the eval
gate, the approval ladder and the audit chain. The CLI talks to the serve process
over ArcUI's account-authenticated HTTP API (see
:mod:`arccli.commands._operator_http`); the server audits every call.

``improve`` always previews first and prints the diff and the gate verdict. It
applies only the previewed candidate (``preview_id``), only when the gate accepted
it, and only after confirmation (``--yes`` or a prompt). ``--dry-run`` stops after
the preview.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from arccli.commands._operator_http import (
    DEFAULT_URL,
    agent_name,
    fail,
    operator_session,
    safe_segment,
    server_url,
)
from arccli.commands._shared import err, print_json, write

#: The server bounds each control (``manual_timeout_s`` ≤ 1h); wait a little longer.
_CONTROL_TIMEOUT_SECONDS = 3660.0


def skill_segment(prog: str, target: str) -> str:
    """The skill name: a skill folder's name, else ``target`` (one safe segment)."""
    path = Path(target).expanduser()
    name = path.resolve().name if path.is_dir() else target
    return safe_segment(prog, name, "skill name")


def _base(prog: str, args: argparse.Namespace, skill: str) -> tuple[str, str]:
    """(server URL, the skill's API base path) — validated before any credential is sent."""
    if not getattr(args, "agent", None) or not getattr(args, "email", None):
        fail(prog, "this runs on the running agent: pass --agent <name> and --email <operator>")
    url = server_url(prog, args.url)
    name = agent_name(prog, args.agent)
    return url, f"/api/agents/{name}/skills/{skill_segment(prog, skill)}"


def confirm(prompt: str) -> bool:
    """Prompt on stdin; EOF declines."""
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        return False


# ---------------------------------------------------------------------------
# arc skill improve
# ---------------------------------------------------------------------------


def improve_handler(args: argparse.Namespace) -> None:
    """Preview (and, when confirmed, apply) one improvement pass on the running agent."""
    prog = "arc skill improve"
    url, base = _base(prog, args, args.skill)
    with operator_session(prog, url, args.email) as call:
        preview = call(
            "POST",
            f"{base}/improve",
            params={"dry_run": "1"},
            json={},
            timeout=_CONTROL_TIMEOUT_SECONDS,
        )
        _show(preview, as_json=args.json)
        if args.dry_run:
            return
        _check_applicable(prog, preview)
        if not args.yes and not confirm("Apply this candidate? [y/N] "):
            fail(prog, "not applied")
        applied = call(
            "POST",
            f"{base}/improve",
            params={"dry_run": "0"},
            json={"confirm": True, "preview_id": preview["preview_id"]},
            timeout=_CONTROL_TIMEOUT_SECONDS,
        )
    _show(applied, as_json=args.json)
    if applied.get("status") != "applied":
        fail(prog, f"not applied: {applied.get('reason') or applied.get('status')}")


def _check_applicable(prog: str, preview: dict[str, Any]) -> None:
    raw_gate = preview.get("gate")
    gate: dict[str, Any] = raw_gate if isinstance(raw_gate, dict) else {}
    if preview.get("status") != "preview":
        fail(prog, f"{preview.get('status')}: {preview.get('reason', '')}")
    if not gate.get("accepted") or not isinstance(preview.get("preview_id"), str):
        fail(prog, f"not applied — the eval gate rejected it: {gate.get('reason', '')}")


def _show(result: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print_json(result)
        return
    write(f"{result.get('skill_name', '')}: {result.get('status', '')}")
    if result.get("reason"):
        write(f"  reason: {result['reason']}")
    gate = result.get("gate")
    if isinstance(gate, dict):
        verdict = "accepted" if gate.get("accepted") else "rejected"
        write(
            f"  eval gate: {verdict} — {gate.get('reason', '')} "
            f"(passing {gate.get('before_pass', 0)} -> {gate.get('after_pass', 0)})"
        )
    if result.get("scores"):
        write(f"  judge scores: {result['scores']}")
    diff = result.get("diff")
    if isinstance(diff, str) and diff:
        write(diff.rstrip("\n"))


# ---------------------------------------------------------------------------
# arc skill evals run / regen (remote half)
# ---------------------------------------------------------------------------


def evals_run(args: argparse.Namespace, skill: str) -> None:
    """Run the golden suite on the running agent; nonzero when any case fails."""
    prog = "arc skill evals run"
    url, base = _base(prog, args, skill)
    with operator_session(prog, url, args.email) as call:
        result = call("POST", f"{base}/evals/run", json={}, timeout=_CONTROL_TIMEOUT_SECONDS)
    if args.json:
        print_json(result)
    else:
        _print_run(result)
    if result.get("status") != "completed":
        fail(prog, f"{result.get('status')}: {result.get('reason', '')}")
    if result.get("failed"):
        err(f"{result['failed']} of {result.get('total')} golden case(s) failed.")
        raise SystemExit(1)


def _print_run(result: dict[str, Any]) -> None:
    write(
        f"{result.get('skill_name', '')}: {result.get('passed', 0)}/{result.get('total', 0)} pass"
    )
    for case in result.get("cases") or []:
        mark = "PASS" if case.get("passed") else "FAIL"
        detail = f"  {case['detail']}" if case.get("detail") else ""
        write(f"  {mark}  {case.get('case_id', '')}{detail}")


def evals_regen(args: argparse.Namespace, skill: str) -> None:
    """Regenerate the machine anchors on the running agent (the caller confirmed)."""
    prog = "arc skill evals regen"
    url, base = _base(prog, args, skill)
    with operator_session(prog, url, args.email) as call:
        result = call(
            "POST", f"{base}/evals/regen", json={"confirm": True}, timeout=_CONTROL_TIMEOUT_SECONDS
        )
    if args.json:
        print_json(result)
    else:
        write(f"{result.get('skill_name', '')}: {result.get('status', '')}")
        if result.get("adopted") is not None:
            adopted, total = result["adopted"], result.get("total")
            write(f"  adopted {adopted} new golden case(s); suite has {total}")
        for item in result.get("quarantined") or []:
            write(f"  quarantined {item.get('nodeid')}: {item.get('reason')}")
        if result.get("reason"):
            write(f"  reason: {result['reason']}")
    if result.get("status") not in ("completed", "no_change"):
        fail(prog, f"{result.get('status')}: {result.get('reason', '')}")


def add_remote_arguments(parser: argparse.ArgumentParser, *, required: bool) -> None:
    """``--agent`` / ``--email`` / ``--url`` for commands that reach the running agent."""
    parser.add_argument(
        "--agent", required=required, default=None, help="Agent directory or roster name."
    )
    parser.add_argument(
        "--email", required=required, default=None, help="Arc operator account email."
    )
    parser.add_argument("--url", default=DEFAULT_URL, help="ArcUI server URL.")


__all__ = ["add_remote_arguments", "evals_regen", "evals_run", "improve_handler"]
