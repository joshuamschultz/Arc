"""CLI commands for the policy module — read-only workspace inspection.

Provides ``arc agent policy <path> ...`` subcommands for viewing policy
bullets, config, and eval history without booting a full agent session.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import click

# Duplicated from PolicyEngine to avoid importing engine + its heavy deps.
# Must stay in sync with policy_engine._BULLET_RE.
_BULLET_RE = re.compile(
    r"^-\s+\[(?P<id>P\d+)\]\s+(?P<text>.+?)\s+"
    r"\{score:(?P<score>\d+),\s*uses:(?P<uses>\d+),\s*"
    r"reviewed:(?P<reviewed>[^,]+),\s*created:(?P<created>[^,]+),\s*"
    r"source:(?P<source>[^}]*)\}",
)


def cli_group(workspace: Path) -> click.Group:
    """Factory: return a Click group bound to *workspace*."""

    @click.group("policy")
    @click.pass_context
    def policy(ctx: click.Context) -> None:
        """Inspect agent policy — bullets, config, history."""
        ctx.ensure_object(dict)
        ctx.obj["workspace"] = workspace

    @policy.command("bullets")
    @click.option(
        "--sort",
        "sort_by",
        type=click.Choice(["score", "created", "reviewed"]),
        default="score",
        help="Sort order for bullets.",
    )
    @click.pass_context
    def bullets(ctx: click.Context, sort_by: str) -> None:
        """List all policy bullets parsed from policy.md."""
        from arccli.formatting import click_echo, print_table

        ws: Path = ctx.obj["workspace"]
        policy_path = ws / "policy.md"
        if not policy_path.exists():
            click_echo("No policy.md found.")
            return

        content = policy_path.read_text(encoding="utf-8")
        parsed = _parse_bullets(content)

        if not parsed:
            click_echo("No structured bullets found in policy.md.")
            return

        # Sort
        if sort_by == "score":
            parsed.sort(key=lambda b: int(b["score"]), reverse=True)
        elif sort_by == "created":
            parsed.sort(key=lambda b: b["created"], reverse=True)
        elif sort_by == "reviewed":
            parsed.sort(key=lambda b: b["reviewed"], reverse=True)

        rows: list[list[str]] = []
        for b in parsed:
            text = b["text"]
            if len(text) > 60:
                text = text[:57] + "..."
            rows.append(
                [
                    b["id"],
                    text,
                    b["score"],
                    b["uses"],
                    b["created"],
                    b["reviewed"],
                ]
            )

        print_table(
            ["ID", "Text", "Score", "Uses", "Created", "Reviewed"],
            rows,
        )

    @policy.command("config")
    @click.pass_context
    def config_cmd(ctx: click.Context) -> None:
        """Show policy configuration from arcagent.toml."""
        from arccli.formatting import click_echo, print_kv

        from arcagent.modules.policy.config import PolicyConfig

        ws: Path = ctx.obj["workspace"]

        # Walk up from workspace to find arcagent.toml
        toml_path = ws.parent / "arcagent.toml"
        loaded_config: dict[str, Any] = {}
        if toml_path.exists():
            with open(toml_path, "rb") as f:
                raw = tomllib.load(f)
            loaded_config = raw.get("modules", {}).get("policy", {}).get("config", {})

        # Merge with defaults
        defaults = PolicyConfig()
        d = defaults
        cfg = loaded_config
        pairs: list[tuple[str, str]] = [
            (
                "eval_interval_turns",
                str(
                    cfg.get("eval_interval_turns", d.eval_interval_turns),
                ),
            ),
            (
                "max_bullets",
                str(
                    cfg.get("max_bullets", d.max_bullets),
                ),
            ),
            (
                "max_bullet_text_length",
                str(
                    cfg.get("max_bullet_text_length", d.max_bullet_text_length),
                ),
            ),
        ]

        click_echo("Policy config (from arcagent.toml + defaults):")
        print_kv(pairs)

    @policy.command("history")
    @click.pass_context
    def history(ctx: click.Context) -> None:
        """Show policy eval history from session transcripts."""
        from arccli.formatting import click_echo

        ws: Path = ctx.obj["workspace"]

        # Policy evals are emitted to telemetry, not persisted to a dedicated
        # audit file yet. Scan session transcripts for policy eval markers.
        sessions_dir = ws / "sessions"
        if not sessions_dir.is_dir():
            click_echo("No sessions directory. Policy eval history unavailable.")
            click_echo("(Policy evals are logged via telemetry during agent runs.)")
            return

        session_files = sorted(
            sessions_dir.glob("*.jsonl"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )

        if not session_files:
            click_echo("No sessions found. Policy eval history unavailable.")
            return

        click_echo("Policy eval events are emitted via telemetry during agent runs.")
        click_echo("Dedicated audit log persistence is planned for a future release.")
        click_echo(f"Sessions available: {len(session_files)}")

    return policy


def _parse_bullets(content: str) -> list[dict[str, str]]:
    """Parse structured bullets from policy.md content."""
    results: list[dict[str, str]] = []
    for line in content.split("\n"):
        match = _BULLET_RE.match(line.strip())
        if match:
            results.append(
                {
                    "id": match.group("id"),
                    "text": match.group("text").strip(),
                    "score": match.group("score"),
                    "uses": match.group("uses"),
                    "reviewed": match.group("reviewed").strip(),
                    "created": match.group("created").strip(),
                    "source": match.group("source").strip(),
                }
            )
    return results
