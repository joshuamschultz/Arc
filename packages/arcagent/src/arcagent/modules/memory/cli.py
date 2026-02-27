"""CLI commands for the memory module — read-only workspace inspection.

Provides ``arc agent memory <path> ...`` subcommands for viewing notes,
entities, search results, and stats without booting a full agent session.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
import yaml


def cli_group(workspace: Path) -> click.Group:
    """Factory: return a Click group bound to *workspace*."""

    @click.group("memory")
    @click.pass_context
    def memory(ctx: click.Context) -> None:
        """Inspect agent memory — notes, entities, search."""
        ctx.ensure_object(dict)
        ctx.obj["workspace"] = workspace

    @memory.command("notes")
    @click.option("--days", default=7, type=int, help="Show notes from last N days.")
    @click.pass_context
    def notes(ctx: click.Context, days: int) -> None:
        """List recent note files and show content."""
        from arccli.formatting import click_echo, print_table

        ws: Path = ctx.obj["workspace"]
        notes_dir = ws / "notes"
        if not notes_dir.is_dir():
            click_echo("No notes directory found.")
            return

        md_files = sorted(notes_dir.glob("*.md"), reverse=True)
        if not md_files:
            click_echo("No notes found.")
            return

        now = datetime.now(UTC)
        rows: list[list[str]] = []
        for f in md_files:
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=UTC)
            age = (now - mtime).days
            if age > days:
                continue
            size_chars = f.stat().st_size
            preview = f.read_text(encoding="utf-8").strip().split("\n")[0][:80]
            rows.append([f.stem, mtime.strftime("%Y-%m-%d"), str(size_chars), preview])

        if rows:
            print_table(["Name", "Date", "Size", "Preview"], rows)
        else:
            click_echo(f"No notes in the last {days} days.")

    @memory.command("entities")
    @click.pass_context
    def entities(ctx: click.Context) -> None:
        """List all entities from markdown files."""
        from arccli.formatting import click_echo, print_table

        ws: Path = ctx.obj["workspace"]
        entities_dir = ws / "entities"
        if not entities_dir.is_dir():
            click_echo("No entities directory found.")
            return

        md_files = sorted(entities_dir.glob("*.md"))
        if not md_files:
            click_echo("No entities found.")
            return

        rows: list[list[str]] = []
        for f in md_files:
            meta = _read_frontmatter(f)
            if meta is None:
                continue
            name = str(meta.get("name", f.stem))
            etype = str(meta.get("type", ""))
            updated = str(meta.get("last_updated", ""))[:10]
            # Count fact lines in body
            body = _strip_frontmatter(f.read_text(encoding="utf-8"))
            fact_count = sum(1 for line in body.split("\n") if line.strip().startswith("- "))
            rows.append([name, etype, str(fact_count), updated])

        if rows:
            print_table(["Name", "Type", "Facts", "Last Updated"], rows)
        else:
            click_echo("No entities found.")

    @memory.command("entity")
    @click.argument("name")
    @click.pass_context
    def entity(ctx: click.Context, name: str) -> None:
        """Show facts for a specific entity."""
        from arccli.formatting import click_echo, print_table

        ws: Path = ctx.obj["workspace"]
        entities_dir = ws / "entities"

        # Slugify the name
        slug = name.lower().strip()
        slug = re.sub(r"[^a-z0-9]+", "-", slug)
        slug = slug.strip("-")

        entity_path = entities_dir / f"{slug}.md"
        if not entity_path.exists():
            click_echo(f"No entity found for '{name}' (slug: {slug}).")
            return

        content = entity_path.read_text(encoding="utf-8")
        meta = _read_frontmatter(entity_path) or {}
        body = _strip_frontmatter(content)

        click_echo(f"Entity: {meta.get('name', name)} ({slug})")
        if meta.get("aliases"):
            click_echo(f"Aliases: {', '.join(meta['aliases'])}")

        rows: list[list[str]] = []
        for line in body.split("\n"):
            line = line.strip()
            if not line.startswith("- "):
                continue
            # Parse "- predicate: value (confidence) [timestamp]"
            fact_match = re.match(
                r"^-\s+(.+?):\s+(.+?)\s+\(([\d.]+)\)\s+\[([^\]]+)\]",
                line,
            )
            if fact_match:
                rows.append(
                    [
                        fact_match.group(1),
                        fact_match.group(2),
                        fact_match.group(3),
                        fact_match.group(4)[:10],
                    ]
                )

        if rows:
            print_table(["Predicate", "Value", "Confidence", "Date"], rows)
        else:
            click_echo(f"No facts for '{name}'.")

    @memory.command("search")
    @click.argument("query")
    @click.option(
        "--scope",
        default=None,
        help="Filter by source type (notes, entities, context).",
    )
    @click.option("--limit", default=10, type=int, help="Max results.")
    @click.pass_context
    def search(ctx: click.Context, query: str, scope: str | None, limit: int) -> None:
        """Search memory using BM25 hybrid search."""
        from arccli.formatting import click_echo, print_table

        from arcagent.modules.memory.config import MemoryConfig
        from arcagent.modules.memory.hybrid_search import HybridSearch

        ws: Path = ctx.obj["workspace"]
        hs = HybridSearch(ws, MemoryConfig())

        async def _run() -> None:
            try:
                results = await hs.search(query, top_k=limit, scope=scope)
            finally:
                await hs.close()

            if not results:
                click_echo("No results found.")
                return

            rows: list[list[str]] = []
            for r in results:
                preview = r.content.replace("\n", " ")[:80]
                rows.append([r.source, f"{r.score:.2f}", r.match_type, preview])

            print_table(["Source", "Score", "Type", "Content"], rows)

        asyncio.run(_run())

    @memory.command("stats")
    @click.pass_context
    def stats(ctx: click.Context) -> None:
        """Show memory workspace statistics."""
        from arccli.formatting import print_kv

        ws: Path = ctx.obj["workspace"]

        # Note count
        notes_dir = ws / "notes"
        note_count = len(list(notes_dir.glob("*.md"))) if notes_dir.is_dir() else 0

        # Entity count (markdown files in entities/)
        entities_dir = ws / "entities"
        entity_count = len(list(entities_dir.glob("*.md"))) if entities_dir.is_dir() else 0

        # Search DB size
        db_path = ws / "search.db"
        db_size = _format_size(db_path.stat().st_size) if db_path.exists() else "n/a"

        # Total workspace size
        total_size = sum(f.stat().st_size for f in ws.rglob("*") if f.is_file())

        print_kv(
            [
                ("Notes", str(note_count)),
                ("Entities", str(entity_count)),
                ("Search DB", db_size),
                ("Workspace size", _format_size(total_size)),
            ]
        )

    return memory


def _read_frontmatter(path: Path) -> dict[str, Any] | None:
    """Parse YAML frontmatter from a markdown file."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    if not text.startswith("---"):
        return None

    end = text.find("\n---", 3)
    if end == -1:
        return None

    fm_text = text[4:end]
    try:
        result: dict[str, Any] = yaml.safe_load(fm_text)
        return result if isinstance(result, dict) else None
    except yaml.YAMLError:
        return None


def _strip_frontmatter(text: str) -> str:
    """Return the body of a markdown file (after frontmatter)."""
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end == -1:
        return text
    body_start = end + 4
    if body_start < len(text) and text[body_start] == "\n":
        body_start += 1
    return text[body_start:]


def _format_size(size_bytes: int) -> str:
    """Format byte count as human-readable size."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
