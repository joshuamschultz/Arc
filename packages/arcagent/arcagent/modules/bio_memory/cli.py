"""CLI commands for the bio-memory module — read-only workspace inspection.

Provides ``arc agent bio-memory <path> ...`` subcommands for viewing
working memory, identity, episodes, and search results.
"""

from __future__ import annotations

from pathlib import Path

import click

from arcagent.utils.sanitizer import read_frontmatter


def cli_group(workspace: Path) -> click.Group:
    """Factory: return a Click group bound to *workspace*."""

    memory_dir = workspace / "memory"

    @click.group("bio-memory")
    @click.pass_context
    def bio_memory(ctx: click.Context) -> None:
        """Inspect bio-memory — working memory, identity, episodes."""
        ctx.ensure_object(dict)
        ctx.obj["workspace"] = workspace
        ctx.obj["memory_dir"] = memory_dir

    @bio_memory.command("status")
    @click.pass_context
    def status(ctx: click.Context) -> None:
        """Show bio-memory workspace overview."""
        md = ctx.obj["memory_dir"]

        # Identity
        identity_path = md / "how-i-work.md"
        identity_exists = identity_path.exists()
        identity_size = identity_path.stat().st_size if identity_exists else 0

        # Working memory
        working_path = md / "working.md"
        working_exists = working_path.exists()

        # Episodes
        episodes_dir = md / "episodes"
        episode_count = (
            len(list(episodes_dir.glob("*.md")))
            if episodes_dir.is_dir()
            else 0
        )

        click.echo("Bio-Memory Status:")
        if identity_exists:
            click.echo(f"  Identity: yes ({identity_size} bytes)")
        else:
            click.echo("  Identity: no")
        click.echo(f"  Working memory: {'active' if working_exists else 'empty'}")
        click.echo(f"  Episodes: {episode_count}")

    @bio_memory.group("identity")
    def identity() -> None:
        """Identity (how-i-work.md) commands."""

    @identity.command("show")
    @click.pass_context
    def identity_show(ctx: click.Context) -> None:
        """Show current identity file."""
        md = ctx.obj["memory_dir"]
        path = md / "how-i-work.md"
        if not path.exists():
            click.echo("No identity file found.")
            return
        click.echo(path.read_text(encoding="utf-8"))

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
