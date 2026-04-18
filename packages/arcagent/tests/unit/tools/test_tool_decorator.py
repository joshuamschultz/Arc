"""SPEC-017 Phase 7 Task 7.1 — ``@tool`` decorator for dynamic tools.

Schema derives from type hints via Pydantic ``validate_call`` pattern;
AI-authored tools never write JSON Schema by hand. Decorator returns
the original function unchanged but stashes metadata on a ``.tool``
attribute for the loader to pick up.
"""

from __future__ import annotations


class TestDecoratorBasics:
    def test_decorator_stashes_metadata(self) -> None:
        from arcagent.tools._decorator import tool

        @tool(description="add two ints")
        async def add(a: int, b: int) -> int:
            return a + b

        assert hasattr(add, "_arc_tool_meta")
        meta = add._arc_tool_meta  # type: ignore[attr-defined]
        assert meta.name == "add"
        assert meta.description == "add two ints"
        # Schema inferred from type hints
        schema = meta.input_schema
        assert schema["type"] == "object"
        assert schema["properties"]["a"]["type"] == "integer"
        assert schema["properties"]["b"]["type"] == "integer"
        assert set(schema["required"]) == {"a", "b"}

    def test_decorator_preserves_callable(self) -> None:
        """Decorated function still invokable with original semantics."""
        from arcagent.tools._decorator import tool

        @tool(description="concat")
        async def concat(a: str, b: str) -> str:
            return a + b

        import asyncio

        result = asyncio.run(concat("hello ", "world"))
        assert result == "hello world"

    def test_classification_defaults_state_modifying(self) -> None:
        from arcagent.tools._decorator import tool

        @tool(description="noop")
        async def noop() -> None:
            return None

        assert noop._arc_tool_meta.classification == "state_modifying"  # type: ignore[attr-defined]

    def test_classification_override(self) -> None:
        from arcagent.tools._decorator import tool

        @tool(description="read a", classification="read_only")
        async def reader(path: str) -> str:
            return f"read {path}"

        assert reader._arc_tool_meta.classification == "read_only"  # type: ignore[attr-defined]

    def test_capability_tags_preserved(self) -> None:
        from arcagent.tools._decorator import tool

        @tool(
            description="fetch",
            classification="read_only",
            capability_tags=["network_egress"],
        )
        async def fetch(url: str) -> str:
            return url

        assert fetch._arc_tool_meta.capability_tags == ["network_egress"]  # type: ignore[attr-defined]

    def test_optional_params_not_required(self) -> None:
        from arcagent.tools._decorator import tool

        @tool(description="opt")
        async def op(a: int, b: int = 5) -> int:
            return a + b

        schema = op._arc_tool_meta.input_schema  # type: ignore[attr-defined]
        assert "a" in schema["required"]
        assert "b" not in schema["required"]


class TestTypeMapping:
    """Python type hints map to JSON Schema types."""

    def test_common_types(self) -> None:
        from arcagent.tools._decorator import tool

        @tool(description="t")
        async def mixed(
            s: str, i: int, f: float, b: bool, li: list, di: dict
        ) -> None:
            return None

        schema = mixed._arc_tool_meta.input_schema  # type: ignore[attr-defined]
        props = schema["properties"]
        assert props["s"]["type"] == "string"
        assert props["i"]["type"] == "integer"
        assert props["f"]["type"] == "number"
        assert props["b"]["type"] == "boolean"
        assert props["li"]["type"] == "array"
        assert props["di"]["type"] == "object"
