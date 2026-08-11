"""`arc agent strategies` — list available execution strategies."""

from __future__ import annotations

import argparse
import sys


def _strategies(_args: argparse.Namespace) -> None:
    """List available execution strategies."""
    import arcrun

    for name, strat in arcrun.available_strategies().items():
        sys.stdout.write(f"  {name}: {strat.description}\n")
