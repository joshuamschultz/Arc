"""Capability: ops — track who owes what by when, and notice when the same step keeps slipping.

The verbs write append-only markdown cards under ``workspace/ops/`` — commitments,
processes, blockers — cross-linked by ``[[slug]]`` so a blocker resolves to the
commitment it holds up and a slip resolves to the process step that produced it.

Two design choices carry the weight:

*Blocked and late are different states.* Late needs a push to the owner; blocked needs a
decision from somebody else. Collapsing them into "not done" is how a blocker sits for a
week while someone politely re-asks the person who was never the constraint.

*A slip is logged against the process step, not just the commitment.* One miss is an
incident. The same step missing three times is a process defect, and it is invisible
unless the misses accumulate somewhere that can be counted. ``ops_slip_patterns`` does
the counting so nobody has to remember across months.

This is *agent state*, so it is written with direct filesystem I/O to the workspace
(ADR-029), never via the ``write`` tool.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from arcagent.builtins.capabilities import _runtime
from arcagent.tools import tool

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(name: str) -> str:
    """Stable lowercase id for an entity name (``'Monthly close'`` -> ``'monthly-close'``)."""
    return _SLUG_RE.sub("-", name.strip().lower()).strip("-") or "unnamed"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M")


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _fields_block(fields: dict[str, str]) -> str:
    """Render non-empty ``key: value`` fields, one per line (values may hold ``[[slug]]``)."""
    return "".join(f"- {key}: {value}\n" for key, value in fields.items() if value)


def _append_card(kind: str, slug: str, title: str, fields: dict[str, str], note: str) -> Path:
    """Create the card if absent, then append a timestamped, structured entry to it."""
    path = _runtime.resolve_workspace_path(f"ops/{kind}/{slug}.md", tool_name="ops")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(f"# {title}\n\n- slug: {slug}\n- type: {kind}\n", encoding="utf-8")
    entry = f"\n## {_now()}\n{_fields_block(fields)}"
    if note:
        entry += f"- note: {note}\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(entry)
    return path


def _link(name: str) -> str:
    """Render a wiki-link to another card, or empty string when no name given."""
    return f"[[{_slug(name)}]]" if name else ""


def _last_field(text: str, key: str) -> str:
    """Return the most recent ``- key: value`` value in a card, or empty string."""
    matches = re.findall(rf"^- {re.escape(key)}: (.+)$", text, re.MULTILINE)
    return matches[-1].strip() if matches else ""


def _all_fields(text: str, key: str) -> list[str]:
    """Every ``- key: value`` value in a card, oldest first — the history, not the latest."""
    return [m.strip() for m in re.findall(rf"^- {re.escape(key)}: (.+)$", text, re.MULTILINE)]


def _cards(kind: str) -> list[Path]:
    """Every card of a kind, oldest slug first; empty when nothing has been logged."""
    directory = _runtime.workspace() / "ops" / kind
    return sorted(directory.glob("*.md")) if directory.is_dir() else []


@tool(
    description="Record a commitment: what, who owns it, and when it is due.",
    capability_tags=["ops"],
    when_to_use="Whenever someone agrees to do something. No owner or date is the finding.",
)
async def ops_log_commitment(
    what: str,
    owner: str = "",
    due: str = "",
    process: str = "",
    status: str = "",
    notes: str = "",
) -> str:
    """Upsert a commitment card under ``workspace/ops/commitments/``."""
    fields = {
        "commitment": what,
        "owner": owner,
        "due": due,
        "process": _link(process),
        "status": status or "open",
    }
    _append_card("commitments", _slug(what), what, fields, notes)
    gaps = [label for label, value in (("owner", owner), ("due date", due)) if not value]
    flag = f"  ⚠ no {' and no '.join(gaps)} — not yet a commitment." if gaps else ""
    return f"Logged commitment {what!r}{flag}"


@tool(
    description="Record a process: its steps, its owner, and how often it runs.",
    capability_tags=["ops"],
    when_to_use="When describing how something should work — a close, a handoff, onboarding.",
)
async def ops_log_process(
    name: str,
    owner: str = "",
    cadence: str = "",
    steps: str = "",
    handoffs: str = "",
    notes: str = "",
) -> str:
    """Upsert a process card under ``workspace/ops/processes/``."""
    fields = {
        "name": name,
        "owner": owner,
        "cadence": cadence,
        "steps": steps,
        "handoffs": handoffs,
    }
    _append_card("processes", _slug(name), name, fields, notes)
    flag = "" if owner else "  ⚠ no owner — an unowned process is the most common root cause."
    return f"Logged process {name!r}{flag}"


@tool(
    description="Record a blocker: what is stuck, and who must decide to unstick it.",
    capability_tags=["ops"],
    when_to_use="When something cannot move until someone else acts. Blocked is not late.",
)
async def ops_log_blocker(
    what: str,
    blocked_on: str = "",
    decider: str = "",
    since: str = "",
    commitment: str = "",
    notes: str = "",
) -> str:
    """Upsert a blocker card, linked to the commitment it holds up."""
    fields = {
        "blocker": what,
        "blocked_on": blocked_on,
        "decider": decider,
        "since": since or _today(),
        "commitment": _link(commitment),
        "status": "blocked",
    }
    _append_card("blockers", _slug(what), what, fields, notes)
    flag = "" if decider else "  ⚠ no decider named — escalation has nowhere to go."
    return f"Logged blocker {what!r}{flag}"


@tool(
    description="Record that a commitment slipped, against the process step that produced it.",
    capability_tags=["ops"],
    when_to_use="Every time a date is missed. One miss is an incident; the count is a defect.",
)
async def ops_log_slip(
    commitment: str,
    process: str = "",
    step: str = "",
    reason: str = "",
    new_due: str = "",
    notes: str = "",
) -> str:
    """Append a slip to the commitment card and to the slip log for pattern counting."""
    fields = {
        "slipped": commitment,
        "process": _link(process),
        "step": step,
        "reason": reason,
        "new_due": new_due,
        "status": "slipped",
    }
    _append_card("commitments", _slug(commitment), commitment, fields, notes)
    _append_card("slips", "slips", "Slips", fields, notes)
    return f"Logged slip on {commitment!r}" + (f" (step: {step})" if step else "")


@tool(
    description="Open commitments: overdue first, by owner, with blockers called out.",
    classification="read_only",
    capability_tags=["ops"],
    when_to_use="For the operating review or morning sweep — what is open and who has it.",
)
async def ops_status() -> str:
    """Report open commitments grouped by state, with unowned and undated ones surfaced."""
    commitments = _cards("commitments")
    if not commitments:
        return "No commitments logged yet."

    today = _today()
    overdue: list[str] = []
    unowned: list[str] = []
    open_items: list[str] = []

    for card in commitments:
        text = card.read_text(encoding="utf-8")
        if _last_field(text, "status") in {"done", "cancelled"}:
            continue
        owner = _last_field(text, "owner")
        due = _last_field(text, "due")
        line = f"- {card.stem}: owner={owner or 'UNOWNED'} due={due or 'NO DATE'}"
        if not owner or not due:
            unowned.append(line)
        elif due < today:
            overdue.append(line)
        else:
            open_items.append(line)

    blocked = []
    for card in _cards("blockers"):
        text = card.read_text(encoding="utf-8")
        if _last_field(text, "status") == "blocked":
            decider = _last_field(text, "decider") or "NO DECIDER"
            blocked.append(f"- {card.stem}: needs a decision from {decider}")

    sections = [
        ("BLOCKED — needs a decision", blocked),
        ("OVERDUE", overdue),
        ("Missing owner or date", unowned),
        ("Open", open_items),
    ]
    out = [f"{title}:\n" + "\n".join(rows) for title, rows in sections if rows]
    return "\n\n".join(out) if out else "Nothing open."


@tool(
    description="Find process steps that slipped repeatedly — a defect, not bad luck.",
    classification="read_only",
    capability_tags=["ops"],
    when_to_use="When the same thing seems to keep going wrong, or on the regular ops review.",
)
async def ops_slip_patterns(threshold: int = 3) -> str:
    """Count slips per process step and report those at or above the repeat threshold."""
    log = _runtime.workspace() / "ops" / "slips" / "slips.md"
    if not log.is_file():
        return "No slips logged yet."

    text = log.read_text(encoding="utf-8")
    steps = [s for s in _all_fields(text, "step") if s]
    processes = [p for p in _all_fields(text, "process") if p]
    if not steps and not processes:
        return "Slips logged, but none name a process step — the pattern cannot be counted."

    lines: list[str] = []
    for label, counts in (("step", Counter(steps)), ("process", Counter(processes))):
        for name, n in counts.most_common():
            if n >= threshold:
                lines.append(
                    f"- {label} {name!r} has slipped {n} times — treat as a process defect."
                )

    if not lines:
        return f"Nothing has slipped {threshold} or more times."
    return "Repeat slips:\n" + "\n".join(lines) + "\n\nName the defect and propose one change."
