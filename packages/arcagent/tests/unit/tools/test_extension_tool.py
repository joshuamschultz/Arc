"""SPEC-017 Phase 7 Task 7.14 — create_extension tool tests."""

from __future__ import annotations

from pathlib import Path

import pytest


_SAFE_SOURCE = (
    "class Demo:\n"
    "    async def startup(self, ctx):\n"
    "        return None\n"
    "    async def shutdown(self):\n"
    "        return None\n"
)

_VALID_YAML = (
    "name: demo\n"
    "version: 0.1.0\n"
    "entry_point: arcagent.modules.demo:Demo\n"
)


class TestFederalDenial:
    async def test_federal_tier_denies_creation(self, tmp_path: Path) -> None:
        from arcagent.core.errors import ToolError
        from arcagent.tools.extension_tools import make_create_extension_tool

        tool = make_create_extension_tool(
            extensions_dir=tmp_path,
            tier="federal",
        )
        with pytest.raises(ToolError) as exc:
            await tool.execute(
                name="demo",
                python_source=_SAFE_SOURCE,
                module_yaml=_VALID_YAML,
            )
        assert exc.value.code == "SELF_MOD_FEDERAL_DENIED"
        # Nothing persisted
        assert list(tmp_path.iterdir()) == []


class TestPersonalSuccess:
    async def test_personal_writes_files(self, tmp_path: Path) -> None:
        from arcagent.tools.extension_tools import make_create_extension_tool

        tool = make_create_extension_tool(
            extensions_dir=tmp_path,
            tier="personal",
        )
        await tool.execute(
            name="demo",
            python_source=_SAFE_SOURCE,
            module_yaml=_VALID_YAML,
        )

        ext_dir = tmp_path / "demo"
        assert (ext_dir / "__init__.py").exists()
        assert (ext_dir / "MODULE.yaml").exists()
        assert "class Demo" in (ext_dir / "__init__.py").read_text()


class TestSourceValidation:
    async def test_malicious_source_rejected(self, tmp_path: Path) -> None:
        from arcagent.tools._dynamic_loader import ASTValidationError
        from arcagent.tools.extension_tools import make_create_extension_tool

        tool = make_create_extension_tool(
            extensions_dir=tmp_path,
            tier="personal",
        )
        with pytest.raises(ASTValidationError):
            await tool.execute(
                name="evil",
                python_source="import os\n",
                module_yaml=_VALID_YAML.replace("name: demo", "name: evil"),
            )


class TestYamlValidation:
    async def test_missing_entry_point_rejected(self, tmp_path: Path) -> None:
        from arcagent.core.errors import ToolError
        from arcagent.tools.extension_tools import make_create_extension_tool

        tool = make_create_extension_tool(
            extensions_dir=tmp_path,
            tier="personal",
        )
        with pytest.raises(ToolError) as exc:
            await tool.execute(
                name="demo",
                python_source=_SAFE_SOURCE,
                module_yaml="name: demo\n",
            )
        assert "entry_point" in str(exc.value)

    async def test_name_mismatch_rejected(self, tmp_path: Path) -> None:
        from arcagent.core.errors import ToolError
        from arcagent.tools.extension_tools import make_create_extension_tool

        tool = make_create_extension_tool(
            extensions_dir=tmp_path,
            tier="personal",
        )
        with pytest.raises(ToolError) as exc:
            await tool.execute(
                name="demo",
                python_source=_SAFE_SOURCE,
                module_yaml=_VALID_YAML.replace("name: demo", "name: other"),
            )
        assert "mismatch" in str(exc.value).lower()


class TestAuditEmission:
    async def test_federal_denial_audits(self, tmp_path: Path) -> None:
        from arcagent.core.errors import ToolError
        from arcagent.tools.extension_tools import make_create_extension_tool

        events: list[tuple[str, dict[str, object]]] = []
        tool = make_create_extension_tool(
            extensions_dir=tmp_path,
            tier="federal",
            audit_sink=lambda e, d: events.append((e, d)),
        )
        with pytest.raises(ToolError):
            await tool.execute(
                name="demo",
                python_source=_SAFE_SOURCE,
                module_yaml=_VALID_YAML,
            )
        denial_events = [e for e in events if e[0] == "self_mod.extension_create_denied"]
        assert len(denial_events) == 1
        assert denial_events[0][1]["tier"] == "federal"

    async def test_personal_creation_audits(self, tmp_path: Path) -> None:
        from arcagent.tools.extension_tools import make_create_extension_tool

        events: list[tuple[str, dict[str, object]]] = []
        tool = make_create_extension_tool(
            extensions_dir=tmp_path,
            tier="personal",
            audit_sink=lambda e, d: events.append((e, d)),
        )
        await tool.execute(
            name="demo",
            python_source=_SAFE_SOURCE,
            module_yaml=_VALID_YAML,
        )
        created = [e for e in events if e[0] == "self_mod.extension_created"]
        assert len(created) == 1
        assert created[0][1]["tier"] == "personal"
