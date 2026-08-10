"""Capability: personal — hold one person's people, promises, and preferences.

The verbs write append-only markdown cards under ``workspace/personal/`` — people,
commitments, preferences — cross-linked by ``[[slug]]`` so "what's going on with the
house" resolves to the contractor, the quote, and the call that was promised.

Two things are deliberately separated. A COMMITMENT is a debt with a direction: either
the operator owes it or someone owes them, and ``ops``-style "not done" collapses that
distinction into uselessness. A PREFERENCE is durable and unprompted — how they like to
be spoken to, what they never want scheduled before 10am — and it is worth its own card
because it should shape every future answer rather than being re-derived each time.

This is *agent state*, so it is written with direct filesystem I/O to the workspace
(ADR-029), never via the ``write`` tool.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from arcagent.builtins.capabilities import _runtime
from arcagent.tools import tool

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(name: str) -> str:
    """Stable lowercase id for an entity name (``'Dr. Reyes'`` -> ``'dr-reyes'``)."""
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
    path = _runtime.resolve_workspace_path(f"personal/{kind}/{slug}.md", tool_name="personal")
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


def _cards(kind: str) -> list[Path]:
    """Every card of a kind, oldest slug first; empty when nothing has been logged."""
    directory = _runtime.workspace() / "personal" / kind
    return sorted(directory.glob("*.md")) if directory.is_dir() else []


@tool(
    description="Record a person: who they are to the operator, and the current thread with them.",
    capability_tags=["personal"],
    when_to_use="Whenever a person is mentioned — family, a doctor, a contractor, a colleague.",
)
async def pa_log_person(
    name: str,
    relationship: str = "",
    context: str = "",
    last_contact: str = "",
    notes: str = "",
) -> str:
    """Upsert a person card under ``workspace/personal/people/``."""
    fields = {
        "name": name,
        "relationship": relationship,
        "context": context,
        "last_contact": last_contact,
    }
    _append_card("people", _slug(name), name, fields, notes)
    return f"Logged {name!r}" + (f" ({relationship})" if relationship else "")


@tool(
    description="Record a promise in either direction: what, who owes it, and by when.",
    capability_tags=["personal"],
    when_to_use="Any time something is promised, either way. A promise is a debt.",
)
async def pa_log_commitment(
    what: str,
    direction: str = "",
    person: str = "",
    due: str = "",
    status: str = "",
    notes: str = "",
) -> str:
    """Upsert a commitment card, recording which way the promise runs."""
    fields = {
        "commitment": what,
        "direction": direction or "i-owe",
        "person": _link(person),
        "due": due,
        "status": status or "open",
    }
    _append_card("commitments", _slug(what), what, fields, notes)
    flag = "" if due else "  ⚠ no date — it will not surface at the right moment."
    return f"Logged commitment {what!r}{flag}"


@tool(
    description="Record a durable preference: how they want things done, standing rules, limits.",
    capability_tags=["personal"],
    when_to_use="When a preference is stated or implied. It should shape every later answer.",
)
async def pa_log_preference(what: str, area: str = "", strength: str = "", notes: str = "") -> str:
    """Append a durable preference to the preferences card."""
    fields = {"preference": what, "area": area, "strength": strength or "stated"}
    _append_card("preferences", "preferences", "Preferences", fields, notes)
    return f"Noted preference: {what!r}"


@tool(
    description="List every open loop: promises owed in each direction, overdue first.",
    classification="read_only",
    capability_tags=["personal"],
    when_to_use="For the daily brief, a weekly sweep, or 'what am I forgetting'.",
)
async def pa_open_loops() -> str:
    """Report open commitments split by direction, with overdue and undated ones surfaced."""
    commitments = _cards("commitments")
    if not commitments:
        return "No open loops logged yet."

    today = _today()
    overdue: list[str] = []
    i_owe: list[str] = []
    owed_to_me: list[str] = []
    undated: list[str] = []

    for card in commitments:
        text = card.read_text(encoding="utf-8")
        if _last_field(text, "status") in {"done", "cancelled"}:
            continue
        due = _last_field(text, "due")
        person = _last_field(text, "person")
        direction = _last_field(text, "direction") or "i-owe"
        line = (
            f"- {card.stem}" + (f" ({person})" if person else "") + (f" due {due}" if due else "")
        )
        if not due:
            undated.append(line)
        elif due < today:
            overdue.append(line)
        elif direction == "i-owe":
            i_owe.append(line)
        else:
            owed_to_me.append(line)

    sections = [
        ("OVERDUE", overdue),
        ("You owe", i_owe),
        ("Owed to you", owed_to_me),
        ("No date set", undated),
    ]
    out = [f"{title}:\n" + "\n".join(rows) for title, rows in sections if rows]
    return "\n\n".join(out) if out else "Nothing open."
