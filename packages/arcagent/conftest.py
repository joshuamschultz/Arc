"""conftest.py — workspace path injection for pytest.

When running pytest from the arcagent package directory, other workspace
packages (arcrun, arcllm, arccli) are not automatically on sys.path.
This conftest adds them so tests can import from sibling packages.
"""

import os
import sys
from pathlib import Path

import pytest

# Workspace root is two levels up from this file
_WORKSPACE_ROOT = Path(__file__).parent.parent.parent

_WORKSPACE_PKGS = [
    "arcrun",
    "arcllm",
    "arccli",
    "arcteam",
    "arcmas",
    "arcgateway",
    "arcmodel",
    "arcprompt",
    "arcskill",
]

for _pkg in _WORKSPACE_PKGS:
    _src = _WORKSPACE_ROOT / "packages" / _pkg / "src"
    if _src.exists() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))


_IN_CI = os.environ.get("CI", "").lower() not in ("", "0", "false")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Gate ``requires_docker`` on a reachable *daemon*, not on the CLI existing.

    ``shutil.which("docker")`` was the old predicate and it lied: OrbStack and
    Docker Desktop leave the CLI on PATH while the daemon is stopped, so the
    isolation suites ran anyway and died on a socket error.

    Locally a stopped daemon skips. In CI it does NOT skip — the tests run and
    fail the build, because these suites are the only evidence that isolation
    holds, and a security suite that silently passes without exercising anything
    is worse than one that is red.
    """
    if _IN_CI:
        return
    from arcrun.backends import DockerBackend

    if DockerBackend.available():
        return
    skip = pytest.mark.skip(reason="no reachable Docker daemon (required in CI)")
    for item in items:
        if "requires_docker" in item.keywords:
            item.add_marker(skip)
