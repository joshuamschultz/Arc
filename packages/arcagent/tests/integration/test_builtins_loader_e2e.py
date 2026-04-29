"""SPEC-021 Phase 2 — end-to-end: CapabilityLoader scans the real builtins.

Points the loader at the actual ``arcagent/builtins/capabilities/``
package directory, runs ``scan_and_register``, and asserts that all
12 builtins (7 file/exec tools + 5 self-mod tools) and 4 skill
folders register successfully.

This is the integration anchor — it proves the new pipeline works
against the production layout we'll wire into the agent in Phase 4.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arcagent.core.capability_loader import CapabilityLoader
from arcagent.core.capability_registry import CapabilityRegistry


@pytest.mark.asyncio
async def test_loader_registers_all_builtins() -> None:
    import arcagent.builtins.capabilities as builtins_pkg

    builtins_root = Path(builtins_pkg.__file__).parent
    skills_root = builtins_root / "skills"

    reg = CapabilityRegistry()
    loader = CapabilityLoader(
        scan_roots=[
            ("builtins", builtins_root),
            ("builtins-skills", skills_root),
        ],
        registry=reg,
    )
    diff = await loader.reload()

    # All 12 tools must register: 7 ports + 5 self-mod.
    expected_tools = {
        "read",
        "write",
        "edit",
        "bash",
        "grep",
        "find",
        "ls",
        "reload",
        "create_tool",
        "create_skill",
        "update_tool",
        "update_skill",
    }
    for name in expected_tools:
        entry = await reg.get_tool(name)
        assert entry is not None, f"{name} did not register; diff: {diff}"

    # All 4 skills must register.
    expected_skills = {"create-tool", "create-skill", "update-tool", "update-skill"}
    for name in expected_skills:
        skill = await reg.get_skill(name)
        assert skill is not None, f"skill {name} did not register; diff: {diff}"
