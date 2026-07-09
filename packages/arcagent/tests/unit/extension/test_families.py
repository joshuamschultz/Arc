"""SPEC-047 Phase 2 — the 4-family registry + inspection over real config + registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from arcagent.core.config import ArcAgentConfig
from arcagent.extension.families import FAMILIES, ScanManyFamily, SelectOneFamily
from arcagent.extension.inspect import inspect_extensions


def _config(modules: dict[str, dict[str, object]] | None = None, tier: str = "personal") -> ArcAgentConfig:
    raw: dict[str, object] = {
        "agent": {"name": "t", "org": "o"},
        "llm": {"model": "anthropic/claude-3-5-sonnet"},
        "security": {"tier": tier},
    }
    if modules is not None:
        raw["modules"] = {name: {"config": cfg} for name, cfg in modules.items()}
    return ArcAgentConfig.model_validate(raw)


def test_four_families_declared_with_correct_kinds() -> None:
    by_name = {f.name: f for f in FAMILIES}
    assert set(by_name) == {"brain", "skills", "tools", "hook-builds"}
    assert isinstance(by_name["brain"], SelectOneFamily)
    assert isinstance(by_name["skills"], SelectOneFamily)
    assert isinstance(by_name["tools"], ScanManyFamily)
    assert isinstance(by_name["hook-builds"], ScanManyFamily)
    assert by_name["tools"].kinds == frozenset({"tool"})
    assert by_name["hook-builds"].kinds == frozenset({"hook", "background_task", "capability"})


def test_select_one_none_is_available_signed_na() -> None:
    rows = inspect_extensions(_config())
    brain = next(r for r in rows if r.family == "brain")
    assert brain.kind == "select_one"
    assert brain.selected == "none"
    assert brain.available is True
    assert brain.signed == "n/a"


def test_select_one_builtin_reports_available() -> None:
    rows = inspect_extensions(_config(modules={"memory": {"brain": "arcmemory"}}))
    brain = next(r for r in rows if r.family == "brain")
    assert brain.selected == "arcmemory"
    assert brain.available is True  # arcmemory is installed in this repo
    assert brain.signed == "builtin"


def test_select_one_byo_refused_above_personal_without_allowlist() -> None:
    rows = inspect_extensions(
        _config(modules={"memory": {"brain": "evil.mod:Brain"}}, tier="federal")
    )
    brain = next(r for r in rows if r.family == "brain")
    assert brain.selected == "evil.mod:Brain"
    assert brain.available is False
    assert brain.signed == "refused"


def test_select_one_byo_allowlisted_above_personal() -> None:
    rows = inspect_extensions(
        _config(
            modules={"memory": {"brain": "evil.mod:Brain", "brain_allowlist": ["evil.mod:Brain"]}},
            tier="federal",
        )
    )
    brain = next(r for r in rows if r.family == "brain")
    assert brain.signed == "allowlisted"


@pytest.mark.asyncio
async def test_scan_many_reports_tools_and_hookbuilds_from_real_registry(tmp_path: Path) -> None:
    from arcagent.capabilities.capability_registry import (
        CapabilityRegistry,
        HookEntry,
        ToolEntry,
    )
    from arcagent.tools._decorator import HookMetadata, ToolMetadata

    reg = CapabilityRegistry()
    tool_src = tmp_path / "mytool.py"
    tool_src.write_text("# tool", encoding="utf-8")
    await reg.register_tool(
        ToolEntry(
            meta=ToolMetadata(
                name="mytool",
                description="d",
                input_schema={"type": "object", "properties": {}},
                classification="read_only",
                capability_tags=(),
                when_to_use="u",
                version="1.0.0",
            ),
            execute=_noop,
            source_path=tool_src,
            scan_root="agent",
        )
    )
    hook_src = tmp_path / "myhook.py"
    hook_src.write_text("# hook", encoding="utf-8")
    await reg.register_hook(
        HookEntry(
            meta=HookMetadata(name="myhook", event="agent:ready", priority=100),
            handler=_noop_hook,
            source_path=hook_src,
            scan_root="builtins",
        )
    )

    rows = inspect_extensions(_config(), registry=reg)
    tools = [r for r in rows if r.family == "tools"]
    hookbuilds = [r for r in rows if r.family == "hook-builds"]
    assert any(r.selected == "mytool" and r.kind == "scan_many" for r in tools)
    assert any(r.selected == "myhook" for r in hookbuilds)
    # unsigned source files → signed == "unsigned"
    assert next(r for r in tools if r.selected == "mytool").signed == "unsigned"


async def _noop(**_: object) -> str:
    return "ok"


async def _noop_hook(**_: object) -> None:
    return None
