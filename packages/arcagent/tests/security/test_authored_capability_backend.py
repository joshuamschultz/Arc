"""Agent-authored (signed + approved) tools must actually RUN once verified.

A signed, TOFU-approved capability-folder tool still executes through
``ArcRunIsolatedRunner``. With no relaxation it routes to the container floor;
on a personal-tier host with the sandbox relaxed off it must run in a bare
subprocess — no Docker required — and round-trip a real call. These tests drive
the real runner (never an injected fake) so the ``relax`` value is proven to
thread from the loader all the way to ArcRun's backend selection.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arcagent.capabilities.capability_loader import CapabilityLoader
from arcagent.capabilities.capability_registry import CapabilityRegistry
from arcagent.capabilities.isolated_tool import ArcRunIsolatedRunner, make_isolated_execute
from arcagent.tools._dynamic_loader import resolve_workspace_import_policy

_SOURCE = (
    "from arcagent.tools._decorator import tool\n"
    "@tool(description='echo')\n"
    "async def echo(value: str) -> str:\n"
    "    return value.upper()\n"
)


@pytest.mark.asyncio
async def test_personal_off_runs_authored_tool_in_a_subprocess() -> None:
    runner = ArcRunIsolatedRunner(tier="personal", relax="off")
    execute = make_isolated_execute(source=_SOURCE, function_name="echo", runner=runner)
    assert await execute(value="hi") == "HI"


@pytest.mark.asyncio
async def test_loader_threads_relax_to_the_real_runner(tmp_path: Path) -> None:
    caps = tmp_path / "capabilities"
    caps.mkdir()
    (caps / "echo.py").write_text(_SOURCE, encoding="utf-8")

    registry = CapabilityRegistry()
    policy = resolve_workspace_import_policy("personal", allow_all_imports=False, allow_imports=[])
    loader = CapabilityLoader(
        scan_roots=[("workspace", caps)],
        registry=registry,
        import_policy=policy,
        isolation_tier="personal",
        isolation_relax="off",
    )
    delta = await loader.scan_and_register()
    assert delta.added == ["echo"]

    entry = await registry.get_tool("echo")
    assert entry is not None
    # No injected runner: this exercises _isolated_runner_for_tool building a real
    # ArcRunIsolatedRunner with the relax value, then a bare-subprocess execution.
    assert await entry.execute(value="round-trip") == "ROUND-TRIP"
