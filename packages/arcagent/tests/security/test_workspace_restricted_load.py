"""SPEC-033 B1 — workspace source loads under RESTRICTED_BUILTINS, not bare exec.

The capability loader must execute workspace-authored ``.py`` with the hardened
namespace (RESTRICTED_BUILTINS + wrapped ``__import__``), never plain
``exec(code, module.__dict__)`` with the full builtin surface. This is the
fast-fail linter in front of the SPEC-036 sandbox — a workspace tool that
reaches for ``open`` at import time must fail to load.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arcagent.capabilities.capability_loader import CapabilityLoader
from arcagent.capabilities.capability_registry import CapabilityRegistry

_BENIGN = (
    "from arcagent.tools._decorator import tool\n"
    "@tool(description='ok', version='1.0.0')\n"
    "async def ok() -> str:\n"
    "    return 'ok'\n"
)

# Passes the AST gate (no blocked import/call) but touches ``open`` — a builtin
# that RESTRICTED_BUILTINS deliberately omits, so it explodes at load.
_USES_OPEN = (
    "from arcagent.tools._decorator import tool\n"
    "_ = open\n"
    "@tool(description='evil', version='1.0.0')\n"
    "async def evil() -> str:\n"
    "    return 'x'\n"
)


def _loader(workspace_caps: Path) -> tuple[CapabilityLoader, CapabilityRegistry]:
    reg = CapabilityRegistry()
    # allow_all_imports=True mirrors personal tier so the arcagent.tools import
    # is never the thing under test — only the builtin surface is.
    loader = CapabilityLoader(
        scan_roots=[("workspace", workspace_caps)],
        registry=reg,
        allow_all_imports=True,
    )
    return loader, reg


@pytest.mark.asyncio
async def test_benign_workspace_tool_loads(tmp_path: Path) -> None:
    caps = tmp_path / "capabilities"
    caps.mkdir()
    (caps / "ok.py").write_text(_BENIGN, encoding="utf-8")
    loader, reg = _loader(caps)
    delta = await loader.scan_and_register()
    assert "ok" in delta.added
    assert not delta.errors


@pytest.mark.asyncio
async def test_workspace_tool_touching_open_fails_to_load(tmp_path: Path) -> None:
    caps = tmp_path / "capabilities"
    caps.mkdir()
    (caps / "evil.py").write_text(_USES_OPEN, encoding="utf-8")
    loader, reg = _loader(caps)
    delta = await loader.scan_and_register()
    # Restricted builtins → NameError on ``open`` at import time → not registered.
    assert "evil" not in delta.added
    assert delta.errors
    assert "NameError" in delta.errors[0][1]
