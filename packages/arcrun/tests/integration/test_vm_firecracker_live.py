"""Live Firecracker microVM execution — Linux + /dev/kvm only.

This is the ONLY test that attempts to boot a real microVM. It is double-guarded:
- @pytest.mark.slow so it is excluded from the fast unit run.
- @pytest.mark.skipif on the absence of /dev/kvm, so it never runs on macOS/Windows
  or any host without hardware virtualization (Firecracker cannot run there).

On a provisioned KVM host a federal agent's execute_python is VM-backed end to end.
Provisioning the guest kernel + rootfs is a federal-deployment prerequisite; where
that is absent the boot fails loudly rather than degrading to a weaker isolation.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from arcrun.builtins.execute import make_execute_tool
from arcrun.events import EventBus
from arcrun.types import ToolContext

_HAS_KVM = os.path.exists("/dev/kvm")

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not _HAS_KVM, reason="no /dev/kvm — Firecracker cannot boot here"),
]


def _ctx() -> ToolContext:
    return ToolContext(
        run_id="vm-live",
        tool_call_id="tc",
        turn_number=1,
        event_bus=EventBus(run_id="vm-live"),
        cancelled=asyncio.Event(),
    )


@pytest.mark.asyncio
async def test_federal_execute_python_runs_in_microvm() -> None:
    tool = make_execute_tool(tier="federal")
    raw = await tool.execute({"code": "print('arc_vm_live')"}, _ctx())
    result = json.loads(raw)
    assert "arc_vm_live" in result["stdout"]
    assert result["exit_code"] == 0
