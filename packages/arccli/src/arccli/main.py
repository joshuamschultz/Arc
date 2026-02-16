"""Root CLI group — `arc` command."""

import click

from arccli.agent import agent
from arccli.ext import ext
from arccli.llm import llm
from arccli.run import run_group
from arccli.skill import skill


@click.group()
def cli() -> None:
    """Arc — unified CLI for Arc products."""


cli.add_command(llm)
cli.add_command(agent)
cli.add_command(run_group)
cli.add_command(ext)
cli.add_command(skill)
