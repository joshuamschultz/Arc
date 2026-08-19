"""Strategies are in-tree drop-ins: the folder is scanned, not a hardcoded list.

The seam the user asked for — add, remove, or replace a file under
``arcrun/strategies/`` and the available set changes to match, with the copy in
markdown like every other prompt. These pin that: every built-in is discovered
by the scan (no central registry to edit), a real file dropped into the package
is picked up end to end, and two strategies claiming one name is a hard error,
never a silent shadow.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import arcrun
from arcrun.strategies import STRATEGIES, Strategy, _register, _strategy_classes

_BUILTINS = {"react", "code", "dynamic", "oneshot", "plan_execute"}


def test_every_builtin_is_found_by_the_scan() -> None:
    found = arcrun.available_strategies()
    assert _BUILTINS <= set(found)
    assert all(isinstance(s, Strategy) for s in found.values())


def test_scan_reads_the_folder_not_a_frozen_list() -> None:
    """Each strategy file's class is discovered from its own module, so the set
    is exactly what the folder holds — proof it is a scan, not a pinned tuple."""
    from arcrun.strategies import oneshot

    names = {c().name for c in _strategy_classes(oneshot)}
    assert "oneshot" in names


def test_a_duplicate_name_is_a_hard_error() -> None:
    class DupA(Strategy):
        @property
        def name(self) -> str:
            return "dup_probe"

        async def __call__(self, model, state, sandbox, max_turns):  # type: ignore[no-untyped-def]
            return None

    class DupB(Strategy):
        @property
        def name(self) -> str:
            return "dup_probe"

        async def __call__(self, model, state, sandbox, max_turns):  # type: ignore[no-untyped-def]
            return None

    with pytest.raises(ValueError, match="duplicate strategy name 'dup_probe'"):
        _register([DupA, DupB])


_PROBE_SOURCE = '''
"""Throwaway strategy dropped in at test time."""
from __future__ import annotations
from typing import Any
from arcrun.strategies import Strategy


class ProbeStrategy(Strategy):
    @property
    def name(self) -> str:
        return "zz_probe_strategy"

    async def __call__(self, model: Any, state: Any, sandbox: Any, max_turns: int) -> Any:
        return None
'''


def test_a_file_dropped_in_the_folder_is_registered(tmp_path: Path) -> None:
    """The literal ask: drop a strategy file in the folder and it works.

    Writes a real module into the package directory, forces a fresh scan, and
    asserts the new strategy is available — then restores the world, so the
    global registry and the source tree are exactly as they were.
    """
    import arcrun.strategies as pkg

    pkg_dir = Path(pkg.__path__[0])
    probe = pkg_dir / "zz_probe_strategy.py"
    saved = dict(STRATEGIES)
    try:
        probe.write_text(_PROBE_SOURCE, encoding="utf-8")
        STRATEGIES.clear()  # force available_strategies() to re-scan the folder

        found = arcrun.available_strategies()

        assert "zz_probe_strategy" in found
        assert found["zz_probe_strategy"].prompt_guidance == ""  # ships no md → empty
    finally:
        probe.unlink(missing_ok=True)
        sys.modules.pop("arcrun.strategies.zz_probe_strategy", None)
        STRATEGIES.clear()
        STRATEGIES.update(saved)
