"""REQ-040 boundary test — arcrun's execute router stays execution-only.

Importing the tier-routed executor must NOT pull ``arcagent`` into the process:
arcrun is a leaf on the execution side and never depends on agent logic. It
receives ``tier``/``relax`` as parameters and never sources them. (arcllm is a
legitimate arcrun foundation dependency and is intentionally not banned here.)
Run in a fresh subprocess for a clean sys.modules assertion.
"""

from __future__ import annotations

import inspect
import subprocess
import sys

from arcrun.builtins.execute import make_execute_tool, resolve_execution_backend

_PROBE = (
    "import arcrun.builtins.execute, sys; "
    "banned = [m for m in sys.modules if m == 'arcagent' or m.startswith('arcagent.')]; "
    "assert not banned, banned; "
    "print('clean')"
)


def test_executor_does_not_import_arcagent() -> None:
    result = subprocess.run(  # noqa: S603 — fixed trusted command, _PROBE is a constant
        [sys.executable, "-c", _PROBE], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "clean" in result.stdout


def test_tier_is_a_parameter_not_sourced() -> None:
    # The router receives tier explicitly; it does not read config or globals.
    params = inspect.signature(resolve_execution_backend).parameters
    assert "tier" in params
    assert "platform_supports_vm" in params
    assert "tier" in inspect.signature(make_execute_tool).parameters
