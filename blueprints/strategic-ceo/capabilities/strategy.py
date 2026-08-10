"""Capability: strategy — hold the long arc as an auditable record of bets and beliefs.

Strategy fails less from bad thinking than from forgetting: the assumption nobody
revisited, the competitor move that was noticed and dropped, the bet that quietly
stopped being true. These verbs write append-only markdown cards under
``workspace/strategy/`` — theses, assumptions, decisions, signals — cross-linked by
``[[slug]]`` so a thesis resolves to the assumptions holding it up and the signals
pressing on it.

The point of separating THESIS from ASSUMPTION is falsifiability. A thesis is what is
believed; an assumption is the specific thing that would have to be true for it to
hold, and therefore the specific thing that can be contradicted. ``strategy_challenge``
records that contradiction against the assumption, so ``strategy_review`` can surface
"this bet is resting on something that stopped being true" without anyone remembering
to ask.

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
    """Stable lowercase id for an entity name (``'Move upmarket'`` -> ``'move-upmarket'``)."""
    return _SLUG_RE.sub("-", name.strip().lower()).strip("-") or "unnamed"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M")


def _fields_block(fields: dict[str, str]) -> str:
    """Render non-empty ``key: value`` fields, one per line (values may hold ``[[slug]]``)."""
    return "".join(f"- {key}: {value}\n" for key, value in fields.items() if value)


def _append_card(kind: str, slug: str, title: str, fields: dict[str, str], note: str) -> Path:
    """Create the card if absent, then append a timestamped, structured entry to it."""
    path = _runtime.resolve_workspace_path(f"strategy/{kind}/{slug}.md", tool_name="strategy")
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
    directory = _runtime.workspace() / "strategy" / kind
    return sorted(directory.glob("*.md")) if directory.is_dir() else []


@tool(
    description="Record a strategic thesis: the bet, why it is believed, and its horizon.",
    capability_tags=["strategy"],
    when_to_use="When a bet or strategic belief is stated — 'we think X, so we do Y'.",
)
async def strategy_log_thesis(
    name: str,
    claim: str = "",
    rationale: str = "",
    horizon: str = "",
    owner: str = "",
    status: str = "",
    notes: str = "",
) -> str:
    """Upsert a thesis card under ``workspace/strategy/theses/``."""
    fields = {
        "name": name,
        "claim": claim,
        "rationale": rationale,
        "horizon": horizon,
        "owner": owner,
        "status": status or "active",
    }
    _append_card("theses", _slug(name), name, fields, notes)
    flag = "" if claim else "  ⚠ no claim stated — a thesis nobody can restate is not a thesis."
    return f"Logged thesis {name!r}{flag}"


@tool(
    description="Record an assumption a thesis rests on, and what evidence would prove it false.",
    capability_tags=["strategy"],
    when_to_use="Whenever a plan depends on something being true. Pair every thesis with these.",
)
async def strategy_log_assumption(
    name: str,
    thesis: str = "",
    statement: str = "",
    falsified_by: str = "",
    confidence: str = "",
    notes: str = "",
) -> str:
    """Upsert an assumption card linked to the thesis it holds up."""
    fields = {
        "name": name,
        "thesis": _link(thesis),
        "statement": statement,
        "falsified_by": falsified_by,
        "confidence": confidence,
        "status": "holding",
    }
    _append_card("assumptions", _slug(name), name, fields, notes)
    flag = "" if falsified_by else "  ⚠ no falsification test — this cannot be checked later."
    return f"Logged assumption {name!r}{flag}"


@tool(
    description="Record evidence that contradicts an assumption, flagging the bets resting on it.",
    capability_tags=["strategy"],
    when_to_use="When something cuts against a stated assumption — the disconfirming case.",
)
async def strategy_challenge(
    assumption: str,
    evidence: str,
    source: str = "",
    verdict: str = "",
    notes: str = "",
) -> str:
    """Append contradicting evidence to an assumption card and mark it challenged."""
    fields = {
        "challenge": evidence,
        "source": source,
        "verdict": verdict or "under review",
        "status": "challenged",
    }
    _append_card("assumptions", _slug(assumption), assumption, fields, notes)
    return f"Challenged assumption {assumption!r} — {verdict or 'under review'}"


@tool(
    description="Record a decision: the choice, the tradeoff accepted, and reversibility.",
    capability_tags=["strategy"],
    when_to_use="When a real choice is made, especially one committing money, people, or time.",
)
async def strategy_log_decision(
    name: str,
    choice: str = "",
    tradeoff: str = "",
    reversible: str = "",
    thesis: str = "",
    revisit_on: str = "",
    notes: str = "",
) -> str:
    """Upsert a decision card linked to the thesis it serves."""
    fields = {
        "name": name,
        "choice": choice,
        "tradeoff": tradeoff,
        "reversible": reversible,
        "thesis": _link(thesis),
        "revisit_on": revisit_on,
    }
    _append_card("decisions", _slug(name), name, fields, notes)
    flag = "" if tradeoff else "  ⚠ no tradeoff named — every real choice costs something."
    return f"Logged decision {name!r}{flag}"


@tool(
    description="Record a market or competitor signal and which thesis it bears on.",
    capability_tags=["strategy"],
    when_to_use="On a competitor move, market shift, or regulatory change worth remembering.",
)
async def strategy_log_signal(
    what: str,
    actor: str = "",
    thesis: str = "",
    implication: str = "",
    source: str = "",
    notes: str = "",
) -> str:
    """Append a signal to the signals log, linked to the thesis it presses on."""
    fields = {
        "signal": what,
        "actor": actor,
        "thesis": _link(thesis),
        "implication": implication,
        "source": source,
    }
    _append_card("signals", "signals", "Signals", fields, notes)
    return f"Logged signal: {what!r}" + (f" (bears on {thesis})" if thesis else "")


@tool(
    description="Review the strategy book: each thesis, its status, and challenged assumptions.",
    classification="read_only",
    capability_tags=["strategy"],
    when_to_use="For a strategy review, or when asked what is currently believed.",
)
async def strategy_review() -> str:
    """Summarize theses and surface the assumptions that have been contradicted."""
    theses = _cards("theses")
    if not theses:
        return "No theses logged yet."

    lines = ["Theses:"]
    for card in theses:
        text = card.read_text(encoding="utf-8")
        status = _last_field(text, "status") or "?"
        claim = _last_field(text, "claim") or "no claim stated ⚠"
        lines.append(f"- {card.stem}: [{status}] {claim}")

    challenged = []
    for card in _cards("assumptions"):
        text = card.read_text(encoding="utf-8")
        if _last_field(text, "status") == "challenged":
            thesis = _last_field(text, "thesis") or "unlinked"
            challenged.append(
                f"- {card.stem} (holds up {thesis}): {_last_field(text, 'challenge')}"
            )

    lines.append("")
    lines.append(
        "Assumptions under challenge:" if challenged else "No assumptions under challenge."
    )
    lines.extend(challenged)
    return "\n".join(lines)
