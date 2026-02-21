"""CLI commands for team memory: arc-memory command."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from arcteam.memory.config import TeamMemoryConfig
from arcteam.memory.service import TeamMemoryService
from arcteam.memory.types import EntityMetadata


def _print_json(data: object) -> None:
    """Print data as formatted JSON."""
    print(json.dumps(data, indent=2, default=str))


def build_memory_parser() -> argparse.ArgumentParser:
    """Build the argument parser for memory subcommands."""
    parser = argparse.ArgumentParser(
        prog="arc-memory",
        description="ArcTeam memory CLI — team knowledge graph",
    )
    parser.add_argument("--root", type=Path, default=None, help="Memory root directory")
    parser.add_argument("--json", action="store_true", help="JSON output mode")

    sub = parser.add_subparsers(dest="command", required=True)

    # status
    sub.add_parser("status", help="Show memory service status")

    # search
    p = sub.add_parser("search", help="Search team memory")
    p.add_argument("query", help="Search query")
    p.add_argument("--max-results", type=int, default=20, help="Max results")

    # entity (subcommands: show, list)
    entity_p = sub.add_parser("entity", help="Entity operations")
    entity_sub = entity_p.add_subparsers(dest="entity_command", required=True)

    show_p = entity_sub.add_parser("show", help="Show entity details")
    show_p.add_argument("entity_id", help="Entity ID")

    list_p = entity_sub.add_parser("list", help="List entities")
    list_p.add_argument("--type", dest="entity_type", default=None, help="Filter by entity type")

    # index (subcommands: rebuild)
    index_p = sub.add_parser("index", help="Index operations")
    index_sub = index_p.add_subparsers(dest="index_command", required=True)
    index_sub.add_parser("rebuild", help="Force index rebuild")

    # promote
    p = sub.add_parser("promote", help="Promote entity to team memory")
    p.add_argument("entity_id", help="Entity ID")
    p.add_argument("--name", required=True, help="Entity name")
    p.add_argument("--type", dest="entity_type", default="person", help="Entity type")
    p.add_argument("--content", default="", help="Markdown content")
    p.add_argument("--tags", default="", help="Comma-separated tags")
    p.add_argument("--links-to", default="", help="Comma-separated linked entity IDs")

    return parser


async def run_memory_command(args: argparse.Namespace) -> int:
    """Execute a memory CLI command. Returns exit code."""
    root = args.root or TeamMemoryConfig().root
    config = TeamMemoryConfig(root=root)
    service = TeamMemoryService(config)

    if args.command == "status":
        status = await service.status()
        _print_json(status.model_dump())

    elif args.command == "search":
        results = await service.search(args.query, max_results=args.max_results)
        if hasattr(args, "json") and args.json:
            _print_json([r.model_dump() for r in results])
        else:
            if not results:
                print("No results found.")
            for r in results:
                print(f"  [{r.score:.2f}] {r.entity_id} — {r.snippet[:60]}")

    elif args.command == "entity":
        if args.entity_command == "show":
            entity = await service.get_entity(args.entity_id)
            if entity is None:
                print(f"Entity not found: {args.entity_id}", file=sys.stderr)
                return 1
            _print_json(entity.metadata.model_dump())
            print("\n" + entity.content)

        elif args.entity_command == "list":
            entries = await service.list_entities(
                entity_type=getattr(args, "entity_type", None)
            )
            if not entries:
                print("No entities found.")
            for e in entries:
                print(f"  {e.entity_id:<30} {e.entity_type:<15} {e.summary_snippet[:40]}")

    elif args.command == "index":
        if args.index_command == "rebuild":
            index = await service.rebuild_index()
            if index is not None:
                print(f"Index rebuilt: {len(index)} entities")
            else:
                print("Memory service is disabled.")

    elif args.command == "promote":
        tags = [t.strip() for t in args.tags.split(",") if t.strip()]
        links = [lnk.strip() for lnk in args.links_to.split(",") if lnk.strip()]
        meta = EntityMetadata(
            entity_type=args.entity_type,
            entity_id=args.entity_id,
            name=args.name,
            tags=tags,
            links_to=links,
        )
        result = await service.promote(
            args.entity_id,
            args.content or f"# {args.name}",
            meta,
            agent_id="cli",
        )
        _print_json(result.model_dump())

    return 0


def main(argv: Sequence[str] | None = None) -> None:
    """Entry point for arc-memory CLI."""
    parser = build_memory_parser()
    args = parser.parse_args(argv)
    exit_code = asyncio.run(run_memory_command(args))
    sys.exit(exit_code)
