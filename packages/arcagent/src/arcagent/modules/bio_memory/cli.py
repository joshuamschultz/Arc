"""CLI commands for the bio-memory module — workspace inspection and management.

Provides ``arc agent bio-memory <path> ...`` subcommands for viewing
working memory, identity, episodes, entities, and triggering consolidation.
"""

from __future__ import annotations

from pathlib import Path

import click

from arcagent.utils.sanitizer import read_frontmatter


class _NoOpTelemetry:
    """Minimal telemetry stub for CLI commands that don't need real telemetry."""

    def audit_event(self, event_type: str, details: dict[str, object] | None = None) -> None:
        """No-op audit event."""
        _ = event_type, details


def cli_group(workspace: Path) -> click.Group:
    """Factory: return a Click group bound to *workspace*."""

    memory_dir = workspace / "memory"

    @click.group("bio-memory")
    @click.pass_context
    def bio_memory(ctx: click.Context) -> None:
        """Inspect bio-memory — working memory, daily notes, episodes, entities."""
        ctx.ensure_object(dict)
        ctx.obj["workspace"] = workspace
        ctx.obj["memory_dir"] = memory_dir

    @bio_memory.command("status")
    @click.pass_context
    def status(ctx: click.Context) -> None:
        """Show bio-memory workspace overview."""
        md = ctx.obj["memory_dir"]
        ws = ctx.obj["workspace"]

        # Working memory
        working_path = md / "working.md"
        working_exists = working_path.exists()

        # Daily notes
        daily_notes_dir = md / "daily-notes"
        daily_note_count = (
            len(list(daily_notes_dir.glob("*.md"))) if daily_notes_dir.is_dir() else 0
        )

        # Episodes
        episodes_dir = md / "episodes"
        episode_count = len(list(episodes_dir.glob("*.md"))) if episodes_dir.is_dir() else 0

        # Entities
        entities_dir = ws / "entities"
        entity_count = len(list(entities_dir.rglob("*.md"))) if entities_dir.is_dir() else 0

        click.echo("Bio-Memory Status:")
        click.echo(f"  Working memory: {'active' if working_exists else 'empty'}")
        click.echo(f"  Daily notes: {daily_note_count}")
        click.echo(f"  Episodes: {episode_count}")
        click.echo(f"  Entities: {entity_count}")

    @bio_memory.group("episodes")
    def episodes() -> None:
        """Episode commands."""

    @episodes.command("list")
    @click.pass_context
    def episodes_list(ctx: click.Context) -> None:
        """List all episodes."""
        md = ctx.obj["memory_dir"]
        episodes_dir = md / "episodes"
        if not episodes_dir.is_dir():
            click.echo("No episodes directory found.")
            return

        md_files = sorted(episodes_dir.glob("*.md"), reverse=True)
        if not md_files:
            click.echo("No episodes found.")
            return

        for f in md_files:
            meta = read_frontmatter(f)
            title = meta.get("title", f.stem) if meta else f.stem
            tags = ", ".join(meta.get("tags", [])) if meta else ""
            click.echo(f"  {f.stem}: {title} [{tags}]")

    @bio_memory.group("working")
    def working() -> None:
        """Working memory commands."""

    @working.command("show")
    @click.pass_context
    def working_show(ctx: click.Context) -> None:
        """Show current working memory."""
        md = ctx.obj["memory_dir"]
        path = md / "working.md"
        if not path.exists():
            click.echo("No working memory file found.")
            return
        text = path.read_text(encoding="utf-8")
        # Show body (strip frontmatter)
        body = _strip_frontmatter(text).strip()
        if body:
            click.echo(body)
        else:
            click.echo("Working memory is empty.")

    @bio_memory.group("entities")
    def entities() -> None:
        """Entity commands."""

    @entities.command("list")
    @click.pass_context
    def entities_list(ctx: click.Context) -> None:
        """List all entity files with frontmatter summary."""
        ws = ctx.obj["workspace"]
        entities_dir = ws / "entities"
        if not entities_dir.is_dir():
            click.echo("No entities directory found.")
            return

        md_files = sorted(entities_dir.rglob("*.md"))
        if not md_files:
            click.echo("No entities found.")
            return

        for f in md_files:
            meta = read_frontmatter(f)
            name = meta.get("name", f.stem) if meta else f.stem
            entity_type = meta.get("entity_type", "?") if meta else "?"
            status = meta.get("status", "?") if meta else "?"
            rel = str(f.relative_to(entities_dir))
            click.echo(f"  {rel}: {name} ({entity_type}) [{status}]")

    @entities.command("normalize")
    @click.pass_context
    def entities_normalize(ctx: click.Context) -> None:
        """Normalize all entity files to v2.1 format (add frontmatter)."""
        ws = ctx.obj["workspace"]
        entities_dir = ws / "entities"
        if not entities_dir.is_dir():
            click.echo("No entities directory found.")
            return

        from arcagent.modules.bio_memory.entity_helpers import normalize_entity_file

        count = 0
        for f in entities_dir.rglob("*.md"):
            meta = read_frontmatter(f)
            if meta is None:
                normalize_entity_file(f, entities_dir)
                count += 1
                click.echo(f"  Normalized: {f.name}")

        click.echo(f"Normalized {count} entity file(s).")

    @bio_memory.command("consolidate-deep")
    @click.option("--dry-run", is_flag=True, help="Preview without writing")
    @click.pass_context
    def consolidate_deep(ctx: click.Context, dry_run: bool) -> None:
        """Trigger deep memory consolidation."""
        import asyncio

        click.echo("Running deep consolidation...")

        async def _run() -> None:
            from arcagent.modules.bio_memory.config import BioMemoryConfig
            from arcagent.modules.bio_memory.deep_consolidator import DeepConsolidator

            ws = ctx.obj["workspace"]
            md = ctx.obj["memory_dir"]
            config = BioMemoryConfig()
            telemetry = _NoOpTelemetry()

            # Validate that DeepConsolidator can be constructed
            DeepConsolidator(
                memory_dir=md,
                workspace=ws,
                config=config,
                telemetry=telemetry,
            )

            # Deep consolidation requires an LLM model
            click.echo(
                "Note: Deep consolidation requires an LLM model. "
                "Use the memory_consolidate_deep tool within an agent session."
            )

        asyncio.run(_run())

    return bio_memory


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
