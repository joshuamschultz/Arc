"""SPEC-017 Phase 7 Tasks 7.9-7.11 — DynamicToolLoader orchestration.

End-to-end dynamic-tool loading:

  1. Encoding check (reject non-UTF-8 declarations)
  2. AST validation (9 bypass categories)
  3. Compile with ``RESTRICTED_BUILTINS``
  4. Extract ``@tool``-decorated callable + metadata
  5. Build a :class:`RegisteredTool`
  6. Apply namespace prefix + collision policy

The loader never inserts modules into ``sys.modules``; each load
creates a fresh module object so reloads do not leak state.
"""

from __future__ import annotations

import pytest

_SAFE_TOOL_SOURCE = """\
from arcagent.tools._decorator import tool

@tool(description="add two ints", classification="read_only")
async def add(a: int, b: int) -> int:
    return a + b
"""

_UNSAFE_IMPORT_SOURCE = """\
import os

async def evil():
    os.system("echo pwn")
"""


class TestSuccessfulLoad:
    def test_safe_tool_loads(self) -> None:
        from arcagent.tools._dynamic_loader import DynamicToolLoader

        loader = DynamicToolLoader()
        tool = loader.load(_SAFE_TOOL_SOURCE, name="add")
        assert tool.name == "add"
        assert tool.description == "add two ints"
        assert tool.classification == "read_only"
        # Namespace prefix applied: agent.{session}.{name}
        assert tool.source.startswith("dynamic:")

    def test_executing_loaded_tool_works(self) -> None:
        import asyncio

        from arcagent.tools._dynamic_loader import DynamicToolLoader

        loader = DynamicToolLoader()
        tool = loader.load(_SAFE_TOOL_SOURCE, name="add")
        result = asyncio.run(tool.execute(a=2, b=3))
        assert result == 5


class TestAstRejection:
    def test_unsafe_import_rejected(self) -> None:
        from arcagent.tools._dynamic_loader import (
            ASTValidationError,
            DynamicToolLoader,
        )

        loader = DynamicToolLoader()
        with pytest.raises(ASTValidationError) as exc:
            loader.load(_UNSAFE_IMPORT_SOURCE, name="evil")
        assert "import:os" in str(exc.value)


class TestSysModulesIsolation:
    """Loader must NOT register the new module in sys.modules."""

    def test_loaded_module_not_in_sys_modules(self) -> None:
        import sys

        from arcagent.tools._dynamic_loader import DynamicToolLoader

        loader = DynamicToolLoader()
        loader.load(_SAFE_TOOL_SOURCE, name="add")

        matching = [k for k in sys.modules if "_agent_tools" in k and "add" in k]
        assert matching == [], f"dynamic modules leaked into sys.modules: {matching}"


class TestCollisionPolicy:
    def test_default_warn_replaces_and_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        """Default policy: ``warn`` replaces the prior registration and logs."""
        from arcagent.tools._dynamic_loader import DynamicToolLoader

        loader = DynamicToolLoader(on_collision="warn")
        loader.load(_SAFE_TOOL_SOURCE, name="add")
        with caplog.at_level("WARNING"):
            loader.load(_SAFE_TOOL_SOURCE, name="add")
        assert any("collision" in rec.message.lower() for rec in caplog.records)

    def test_error_policy_raises(self) -> None:
        from arcagent.core.errors import ToolError
        from arcagent.tools._dynamic_loader import DynamicToolLoader

        loader = DynamicToolLoader(on_collision="error")
        loader.load(_SAFE_TOOL_SOURCE, name="add")
        with pytest.raises(ToolError):
            loader.load(_SAFE_TOOL_SOURCE, name="add")


class TestNoDecoratedToolFound:
    def test_source_without_tool_decorator_raises(self) -> None:
        from arcagent.core.errors import ToolError
        from arcagent.tools._dynamic_loader import DynamicToolLoader

        loader = DynamicToolLoader()
        src = "async def orphan(a: int) -> int:\n    return a\n"
        with pytest.raises(ToolError) as exc:
            loader.load(src, name="orphan")
        assert "decorated with @tool" in str(exc.value)


class TestAuditEmission:
    """Every load emits an audit event — SPEC-017 R-058."""

    def test_successful_load_audits(self) -> None:
        from arcagent.tools._dynamic_loader import DynamicToolLoader

        events: list[tuple[str, dict[str, object]]] = []
        loader = DynamicToolLoader(audit_sink=lambda e, d: events.append((e, d)))
        loader.load(_SAFE_TOOL_SOURCE, name="add")
        assert any(e[0] == "dynamic_tool.loaded" for e in events)
        payload = next(e[1] for e in events if e[0] == "dynamic_tool.loaded")
        assert payload["name"] == "add"
        assert "content_hash" in payload

    def test_rejected_load_audits(self) -> None:
        from arcagent.tools._dynamic_loader import (
            ASTValidationError,
            DynamicToolLoader,
        )

        events: list[tuple[str, dict[str, object]]] = []
        loader = DynamicToolLoader(audit_sink=lambda e, d: events.append((e, d)))
        with pytest.raises(ASTValidationError):
            loader.load(_UNSAFE_IMPORT_SOURCE, name="evil")
        assert any(e[0] == "dynamic_tool.rejected" for e in events)
