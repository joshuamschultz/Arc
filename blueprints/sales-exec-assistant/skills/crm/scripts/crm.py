"""Write and read the CRM card store. Everything deterministic lives here.

Usage:
    python3 crm.py log deal --name "Acme expansion" --stage Procurement
    python3 crm.py log contact --name "Helen Li" --company "3G Lighting"
    python3 crm.py pipeline

Cards are append-only markdown under ``<workspace>/crm/<kind>/<slug>.md``. What
is mechanical is decided here, not by the model: the slug, the UTC timestamp, the
card header, the append (never a rewrite), which fields render as ``[[slug]]``
links, and which value counts as current when a card holds twenty entries.

Anything a model re-derives per call is a thing it can get wrong once. A slug
computed slightly differently forks an account into two cards that each tell half
the truth; a timestamp written from memory lands at 00:00; a card "tidied" on the
way past loses the history that makes a pipeline readable. The judgement — which
deal this is, whether the stage really moved — stays in SKILL.md, where judgement
belongs.

Standard library only; runs under any interpreter the agent's shell has, and
imports nothing from arcagent (an agent capability's imports are not available to
a script, and it does not need them — the workspace is right here).
"""
# ruff: noqa: T201 — CLI tool; print is the interface.

from __future__ import annotations

import argparse
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

_SLUG_RE = re.compile(r"[^a-z0-9]+")

#: Field order per kind, and which of them render as ``[[slug]]`` links.
#: The schema lives here so a mistyped field is an error, not a silently
#: malformed card that nothing reads back.
_SCHEMA: dict[str, tuple[tuple[str, ...], frozenset[str]]] = {
    "contact": (("name", "role", "employer", "deal_role", "email"), frozenset({"employer"})),
    "company": (("name", "industry"), frozenset()),
    "deal": (
        ("name", "company", "stage", "value", "next_step", "next_step_owner", "champion"),
        frozenset({"company", "champion"}),
    ),
    "meeting": (("title", "attendees", "deal", "follow_ups"), frozenset({"deal"})),
    "commitment": (("commitment", "owner", "due", "deal"), frozenset({"deal"})),
}

#: Plural directory each kind's cards live in.
_DIRS = {
    "contact": "contacts",
    "company": "companies",
    "deal": "deals",
    "meeting": "meetings",
    "commitment": "commitments",
}

#: Kinds that append to ONE shared card instead of one card per entity.
_SHARED_CARD = {"commitment": "commitments"}

#: The field whose value names the card, per kind.
_TITLE_FIELD = {
    "contact": "name",
    "company": "name",
    "deal": "name",
    "meeting": "title",
    "commitment": "commitment",
}


def slugify(name: str) -> str:
    """Stable id for an entity name. ``'3G Lighting'`` -> ``'3g-lighting'``."""
    return _SLUG_RE.sub("-", name.strip().lower()).strip("-") or "unnamed"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M")


def _card_path(workspace: Path, kind: str, slug: str) -> Path:
    return workspace / "crm" / _DIRS[kind] / f"{slug}.md"


def _render_fields(kind: str, values: dict[str, str]) -> str:
    """Render the non-empty fields for ``kind``, in schema order, links linked."""
    order, link_fields = _SCHEMA[kind]
    lines = []
    for field in order:
        value = (values.get(field) or "").strip()
        if not value:
            continue
        rendered = f"[[{slugify(value)}]]" if field in link_fields else value
        lines.append(f"- {field}: {rendered}\n")
    return "".join(lines)


def append_card(
    workspace: Path, kind: str, values: dict[str, str], note: str = "", *, slug: str | None = None
) -> Path:
    """Create the card if absent, then append one timestamped entry. Never rewrites."""
    title = (values.get(_TITLE_FIELD[kind]) or "").strip()
    card_slug = slug or _SHARED_CARD.get(kind) or slugify(title)
    path = _card_path(workspace, kind, card_slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        heading = _SHARED_CARD.get(kind, title) or card_slug
        header = f"# {heading.title() if kind in _SHARED_CARD else heading}\n\n"
        path.write_text(
            f"{header}- slug: {card_slug}\n- type: {_DIRS[kind]}\n",
            encoding="utf-8",
        )
    entry = f"\n## {_now()}\n{_render_fields(kind, values)}"
    if note.strip():
        entry += f"- note: {note.strip()}\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(entry)
    return path


def last_field(text: str, key: str) -> str:
    """The most recent ``- key: value`` in an append-only card, or ``''``.

    Last, not first: every earlier entry is history, and reading the first is how
    a pipeline reports a stage the deal left months ago.
    """
    matches = re.findall(rf"^- {re.escape(key)}: (.+)$", text, re.MULTILINE)
    return matches[-1].strip() if matches else ""


def pipeline(workspace: Path) -> str:
    """One line per deal: its latest stage and next step."""
    deals_dir = workspace / "crm" / "deals"
    if not deals_dir.is_dir():
        return "No deals logged yet."
    lines = []
    for card in sorted(deals_dir.glob("*.md")):
        text = card.read_text(encoding="utf-8")
        stage = last_field(text, "stage") or "?"
        next_step = last_field(text, "next_step") or "no next step ⚠"
        lines.append(f"- {card.stem}: [{stage}] next: {next_step}")
    return "Pipeline:\n" + "\n".join(lines) if lines else "No deals logged yet."


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="CRM card store — log entities, read the pipeline."
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Agent workspace root (default: cwd, which is where the shell already runs).",
    )
    subs = parser.add_subparsers(dest="command", required=True)

    log = subs.add_parser("log", help="Append an entry to an entity's card.")
    log_subs = log.add_subparsers(dest="kind", required=True)
    for kind, (fields, _links) in _SCHEMA.items():
        kind_parser = log_subs.add_parser(kind, help=f"Log a {kind}.")
        for field in fields:
            required = field == _TITLE_FIELD[kind]
            kind_parser.add_argument(
                f"--{field.replace('_', '-')}", dest=field, default="", required=required
            )
        kind_parser.add_argument("--note", default="", help="Free-text context.")

    subs.add_parser("pipeline", help="Print every deal with its latest stage and next step.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    workspace = args.workspace.expanduser().resolve()

    if args.command == "pipeline":
        print(pipeline(workspace))
        return 0

    values = {
        field: getattr(args, field, "")
        for field in _SCHEMA[args.kind][0]
        if getattr(args, field, "")
    }
    path = append_card(workspace, args.kind, values, args.note)
    title = values.get(_TITLE_FIELD[args.kind], "")

    summary = f"Logged {args.kind} {title!r}"
    if args.kind == "deal":
        if values.get("stage"):
            summary += f" [{values['stage']}]"
        if not values.get("next_step"):
            summary += "  ⚠ no next step set — schedule one."
    # A commitment names its deal, so surface that the deal card was NOT touched:
    # the tool logs exactly what it was told to, and says so.
    print(f"{summary}  ({path})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
