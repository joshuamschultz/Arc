"""`arc agent strategies` — list available execution strategies."""

from __future__ import annotations

import argparse
import sys


def _strategies(_args: argparse.Namespace) -> None:
    """List available execution strategies."""
    from arcrun.strategies import STRATEGIES, _load_strategies

    if not STRATEGIES:
        _load_strategies()
    for name, strat in STRATEGIES.items():
        sys.stdout.write(f"  {name}: {strat.description}\n")
