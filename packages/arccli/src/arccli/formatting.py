"""Output formatting helpers — ASCII tables, JSON, key-value pairs."""

import json
from typing import Any


def print_table(headers: list[str], rows: list[list[str]]) -> None:
    """Print an aligned ASCII table to stdout."""
    if not rows:
        click_echo("(no data)")
        return

    # Calculate column widths (max of header and all row values)
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(str(cell)))

    # Format header
    header_line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    separator = "  ".join("-" * w for w in widths)

    click_echo(header_line)
    click_echo(separator)
    for row in rows:
        line = "  ".join(str(cell).ljust(w) for cell, w in zip(row, widths))
        click_echo(line)


def print_json(data: Any) -> None:
    """Print data as indented JSON to stdout."""
    click_echo(json.dumps(data, indent=2, default=str))


def print_kv(pairs: list[tuple[str, str]]) -> None:
    """Print key-value pairs with aligned colons."""
    if not pairs:
        return
    max_key = max(len(k) for k, _ in pairs)
    for key, value in pairs:
        click_echo(f"  {key.ljust(max_key)} : {value}")


def click_echo(msg: str = "") -> None:
    """Wrapper around click.echo for testability."""
    import click

    click.echo(msg)
