"""Capability: crm — capture the revenue book of business as interlinked cards.

The verbs write structured, append-only markdown cards under ``workspace/crm/`` —
contacts, companies, deals, meetings — and cross-link them by ``[[slug]]`` so a deal
resolves to its company, champion, and meetings. This is *agent state*, so it is
written with direct filesystem I/O to the workspace (ADR-029), never via the ``write``
tool. The distiller's revenue-lens prompts capture the same entities from conversation;
these verbs give the executive a deterministic "log this now" that the memory can read.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from arcagent.builtins.capabilities import _runtime
from arcagent.tools import tool

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(name: str) -> str:
    """Stable lowercase id for an entity name (``'Acme Corp'`` -> ``'acme-corp'``)."""
    return _SLUG_RE.sub("-", name.strip().lower()).strip("-") or "unnamed"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M")


def _fields_block(fields: dict[str, str]) -> str:
    """Render non-empty ``key: value`` fields, one per line (values may hold ``[[slug]]``)."""
    return "".join(f"- {key}: {value}\n" for key, value in fields.items() if value)


def _append_card(kind: str, slug: str, title: str, fields: dict[str, str], note: str) -> Path:
    """Create the card if absent, then append a timestamped, structured entry to it."""
    path = _runtime.resolve_workspace_path(f"crm/{kind}/{slug}.md", tool_name="crm")
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


@tool(
    description="Record or update a contact (person) in the CRM, linked to their company.",
    capability_tags=["crm"],
    when_to_use="When the executive mentions a person: a buyer, champion, or new contact.",
)
async def crm_log_contact(
    name: str,
    company: str = "",
    role: str = "",
    deal_role: str = "",
    email: str = "",
    notes: str = "",
) -> str:
    """Upsert a contact card under ``workspace/crm/contacts/`` linked to their company."""
    fields = {
        "name": name,
        "role": role,
        "employer": _link(company),
        "deal_role": deal_role,
        "email": email,
    }
    _append_card("contacts", _slug(name), name, fields, notes)
    return f"Logged contact {name!r}" + (f" @ {company}" if company else "")


@tool(
    description="Record or update a company (account) in the CRM.",
    capability_tags=["crm"],
    when_to_use="When the executive mentions an account or prospect company.",
)
async def crm_log_company(name: str, industry: str = "", notes: str = "") -> str:
    """Upsert a company card under ``workspace/crm/companies/``."""
    _append_card("companies", _slug(name), name, {"name": name, "industry": industry}, notes)
    return f"Logged company {name!r}"


@tool(
    description="Record/update a deal: stage, value, next step, champion — linked to its company.",
    capability_tags=["crm"],
    when_to_use="When a deal is mentioned or moves — new opportunity, stage change, or next step.",
)
async def crm_log_deal(
    name: str,
    company: str = "",
    stage: str = "",
    value: str = "",
    next_step: str = "",
    owner: str = "",
    champion: str = "",
    notes: str = "",
) -> str:
    """Upsert a deal card under ``workspace/crm/deals/`` linked to company + champion."""
    fields = {
        "name": name,
        "company": _link(company),
        "stage": stage,
        "value": value,
        "next_step": next_step,
        "next_step_owner": owner,
        "champion": _link(champion),
    }
    _append_card("deals", _slug(name), name, fields, notes)
    flag = "" if next_step else "  ⚠ no next step set — schedule one."
    return f"Logged deal {name!r}" + (f" [{stage}]" if stage else "") + flag


@tool(
    description="Record a meeting: attendees, the deal it advanced, notes, and follow-ups.",
    capability_tags=["crm"],
    when_to_use="After a call or meeting, to capture what happened and what is owed next.",
)
async def crm_log_meeting(
    title: str,
    attendees: str = "",
    deal: str = "",
    notes: str = "",
    follow_ups: str = "",
) -> str:
    """Upsert a meeting card under ``workspace/crm/meetings/`` linked to its deal."""
    slug = f"{_slug(title)}-{datetime.now(UTC).strftime('%Y%m%d')}"
    fields = {
        "title": title,
        "attendees": attendees,
        "deal": _link(deal),
        "follow_ups": follow_ups,
    }
    _append_card("meetings", slug, title, fields, notes)
    tail = "  (follow-ups captured)" if follow_ups else "  ⚠ no follow-up captured."
    return f"Logged meeting {title!r}{tail}"


@tool(
    description="Note a commitment (who owes what, by when) on a deal — tracked till settled.",
    capability_tags=["crm"],
    when_to_use="Whenever a promise is made either way: a follow-up, a send, a next step.",
)
async def crm_note_commitment(
    what: str, who: str = "", due: str = "", deal: str = ""
) -> str:
    """Append a commitment to the commitments log (and the deal card when named)."""
    fields = {"commitment": what, "owner": who, "due": due, "deal": _link(deal)}
    _append_card("commitments", "commitments", "Commitments", fields, "")
    if deal:
        _append_card("deals", _slug(deal), deal, {"commitment": what, "due": due}, "")
    return f"Noted commitment: {what!r}" + (f" (due {due})" if due else "")


@tool(
    description="Summarize the open pipeline: each deal with its latest stage and next step.",
    classification="read_only",
    capability_tags=["crm"],
    when_to_use="For a pipeline review or the morning briefing — what's open, where it stands.",
)
async def crm_pipeline() -> str:
    """Read every deal card and return a one-line-per-deal pipeline summary."""
    deals_dir = _runtime.workspace() / "crm" / "deals"
    if not deals_dir.is_dir():
        return "No deals logged yet."
    lines: list[str] = []
    for card in sorted(deals_dir.glob("*.md")):
        text = card.read_text(encoding="utf-8")
        stage = _last_field(text, "stage") or "?"
        nxt = _last_field(text, "next_step") or "no next step ⚠"
        lines.append(f"- {card.stem}: [{stage}] next: {nxt}")
    return "Pipeline:\n" + "\n".join(lines) if lines else "No deals logged yet."


def _last_field(text: str, key: str) -> str:
    """Return the most recent ``- key: value`` value in a card, or empty string."""
    matches = re.findall(rf"^- {re.escape(key)}: (.+)$", text, re.MULTILINE)
    return matches[-1].strip() if matches else ""
