"""Legacy Click-based entry point — preserved for backward compatibility.

This module is intentionally kept as-is so that the arc-legacy entry point
and internal handler dispatch stubs (registry.py) can delegate to it during
the T1.1.5 migration phase.

DO NOT add new commands here. Add them to arccli.commands.registry instead.
"""

import click

from arccli.agent import agent
from arccli.ext import ext
from arccli.init_wizard import init
from arccli.llm import llm
from arccli.run import run_group
from arccli.skill import skill
from arccli.team import team
from arccli.ui import ui


@click.group()
def cli() -> None:
    """Arc — unified CLI for Arc products (legacy Click interface)."""


cli.add_command(init)
cli.add_command(llm)
cli.add_command(agent)
cli.add_command(run_group)
cli.add_command(ext)
cli.add_command(skill)
cli.add_command(team)
cli.add_command(ui)
