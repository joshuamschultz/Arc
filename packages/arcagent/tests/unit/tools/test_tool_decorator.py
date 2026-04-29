"""SPEC-021 Task 1.1 — extended ``@tool`` decorator metadata.

The decorator stamps ``func._arc_capability_meta`` with a
:class:`ToolMetadata` instance. Schema is inferred from typed
signatures. New fields (when_to_use, requires_skill, version, examples,
model_hint) default to empty/None so existing usage keeps working.
"""

from __future__ import annotations


class TestDecoratorBasics:
    def test_decorator_stashes_metadata(self) -> None:
        from arcagent.tools._decorator import tool

        @tool(description="add two ints")
        async def add(a: int, b: int) -> int:
            return a + b

        assert hasattr(add, "_arc_capability_meta")
        meta = add._arc_capability_meta  # type: ignore[attr-defined]
        assert meta.kind == "tool"
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

        assert noop._arc_capability_meta.classification == "state_modifying"  # type: ignore[attr-defined]

    def test_classification_override(self) -> None:
        from arcagent.tools._decorator import tool

        @tool(description="read a", classification="read_only")
        async def reader(path: str) -> str:
            return f"read {path}"

        assert reader._arc_capability_meta.classification == "read_only"  # type: ignore[attr-defined]

    def test_capability_tags_preserved(self) -> None:
        from arcagent.tools._decorator import tool

        @tool(
            description="fetch",
            classification="read_only",
            capability_tags=["network_egress"],
        )
        async def fetch(url: str) -> str:
            return url

        assert fetch._arc_capability_meta.capability_tags == ("network_egress",)  # type: ignore[attr-defined]

    def test_optional_params_not_required(self) -> None:
        from arcagent.tools._decorator import tool

        @tool(description="opt")
        async def op(a: int, b: int = 5) -> int:
            return a + b

        schema = op._arc_capability_meta.input_schema  # type: ignore[attr-defined]
        assert "a" in schema["required"]
        assert "b" not in schema["required"]


class TestTypeMapping:
    """Python type hints map to JSON Schema types."""

    def test_common_types(self) -> None:
        from arcagent.tools._decorator import tool

        @tool(description="t")
        async def mixed(s: str, i: int, f: float, b: bool, li: list, di: dict) -> None:
            return None

        schema = mixed._arc_capability_meta.input_schema  # type: ignore[attr-defined]
        props = schema["properties"]
        assert props["s"]["type"] == "string"
        assert props["i"]["type"] == "integer"
        assert props["f"]["type"] == "number"
        assert props["b"]["type"] == "boolean"
        assert props["li"]["type"] == "array"
        assert props["di"]["type"] == "object"


class TestExtendedToolFields:
    """SPEC-021 R-003, R-033 — new fields on ToolMetadata."""

    def test_when_to_use_default_empty(self) -> None:
        from arcagent.tools._decorator import tool

        @tool(description="d")
        async def fn() -> None:
            return None

        assert fn._arc_capability_meta.when_to_use == ""  # type: ignore[attr-defined]

    def test_when_to_use_set(self) -> None:
        from arcagent.tools._decorator import tool

        @tool(description="d", when_to_use="when you need X")
        async def fn() -> None:
            return None

        assert fn._arc_capability_meta.when_to_use == "when you need X"  # type: ignore[attr-defined]

    def test_requires_skill_default_none(self) -> None:
        from arcagent.tools._decorator import tool

        @tool(description="d")
        async def fn() -> None:
            return None

        assert fn._arc_capability_meta.requires_skill is None  # type: ignore[attr-defined]

    def test_requires_skill_set(self) -> None:
        from arcagent.tools._decorator import tool

        @tool(description="d", requires_skill="create-tool")
        async def fn() -> None:
            return None

        assert fn._arc_capability_meta.requires_skill == "create-tool"  # type: ignore[attr-defined]

    def test_version_defaults_1_0_0(self) -> None:
        from arcagent.tools._decorator import tool

        @tool(description="d")
        async def fn() -> None:
            return None

        assert fn._arc_capability_meta.version == "1.0.0"  # type: ignore[attr-defined]

    def test_version_set(self) -> None:
        from arcagent.tools._decorator import tool

        @tool(description="d", version="2.3.1")
        async def fn() -> None:
            return None

        assert fn._arc_capability_meta.version == "2.3.1"  # type: ignore[attr-defined]

    def test_examples_default_empty_tuple(self) -> None:
        from arcagent.tools._decorator import tool

        @tool(description="d")
        async def fn() -> None:
            return None

        assert fn._arc_capability_meta.examples == ()  # type: ignore[attr-defined]

    def test_examples_set(self) -> None:
        from arcagent.tools._decorator import tool

        @tool(description="d", examples=["fn()", "fn(x=1)"])
        async def fn(x: int = 0) -> None:
            return None

        assert fn._arc_capability_meta.examples == ("fn()", "fn(x=1)")  # type: ignore[attr-defined]

    def test_model_hint_default_none(self) -> None:
        from arcagent.tools._decorator import tool

        @tool(description="d")
        async def fn() -> None:
            return None

        assert fn._arc_capability_meta.model_hint is None  # type: ignore[attr-defined]

    def test_model_hint_set(self) -> None:
        from arcagent.tools._decorator import tool

        @tool(description="d", model_hint="haiku")
        async def fn() -> None:
            return None

        assert fn._arc_capability_meta.model_hint == "haiku"  # type: ignore[attr-defined]

    def test_metadata_is_frozen(self) -> None:
        """Metadata is immutable post-stamp — defends against poisoning."""
        from dataclasses import FrozenInstanceError

        import pytest

        from arcagent.tools._decorator import tool

        @tool(description="d")
        async def fn() -> None:
            return None

        meta = fn._arc_capability_meta  # type: ignore[attr-defined]
        with pytest.raises(FrozenInstanceError):
            meta.version = "9.9.9"  # type: ignore[misc]
