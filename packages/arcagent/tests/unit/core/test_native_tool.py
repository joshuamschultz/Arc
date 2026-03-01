"""Tests for the native_tool decorator."""

from __future__ import annotations

from typing import Any

import pytest

from arcagent.core.tool_registry import ToolTransport, native_tool


class TestNativeToolDecorator:
    def test_basic_decorator(self) -> None:
        @native_tool(description="Echo text", source="test")
        async def echo(text: str = "", **kwargs: Any) -> str:
            return f"echo: {text}"

        assert hasattr(echo, "tool")
        assert echo.tool.name == "echo"
        assert echo.tool.description == "Echo text"
        assert echo.tool.source == "test"
        assert echo.tool.transport == ToolTransport.NATIVE

    def test_custom_name(self) -> None:
        @native_tool(name="my_echo", description="Echo")
        async def echo(**kwargs: Any) -> str:
            return ""

        assert echo.tool.name == "my_echo"

    def test_schema_from_signature(self) -> None:
        @native_tool(
            description="Test",
            params={"path": "File path to read"},
            required=["path"],
        )
        async def read_file(path: str = "", **kwargs: Any) -> str:
            return ""

        schema = read_file.tool.input_schema
        assert schema["type"] == "object"
        assert "path" in schema["properties"]
        assert schema["properties"]["path"]["type"] == "string"
        assert schema["properties"]["path"]["description"] == "File path to read"
        assert schema["required"] == ["path"]

    def test_schema_with_dict_params(self) -> None:
        @native_tool(
            description="Test",
            params={
                "status": {
                    "type": "string",
                    "enum": ["pending", "done"],
                    "description": "Filter status",
                },
            },
        )
        async def list_tasks(status: str = "", **kwargs: Any) -> str:
            return ""

        prop = list_tasks.tool.input_schema["properties"]["status"]
        assert prop["type"] == "string"
        assert prop["enum"] == ["pending", "done"]
        assert prop["description"] == "Filter status"

    def test_type_inference_from_annotation(self) -> None:
        @native_tool(description="Test")
        async def calc(x: int = 0, y: float = 0.0, flag: bool = False, **kw: Any) -> str:
            return ""

        props = calc.tool.input_schema["properties"]
        assert props["x"]["type"] == "integer"
        assert props["y"]["type"] == "number"
        assert props["flag"]["type"] == "boolean"

    def test_type_inference_from_default(self) -> None:
        @native_tool(description="Test")
        async def func(name="", count=0, **kw: Any) -> str:
            return ""

        props = func.tool.input_schema["properties"]
        assert props["name"]["type"] == "string"
        assert props["count"]["type"] == "integer"

    def test_kwargs_excluded(self) -> None:
        @native_tool(description="Test")
        async def func(name: str = "", **kwargs: Any) -> str:
            return ""

        props = func.tool.input_schema["properties"]
        assert "kwargs" not in props

    def test_timeout_seconds(self) -> None:
        @native_tool(description="Test", timeout_seconds=60)
        async def slow(**kw: Any) -> str:
            return ""

        assert slow.tool.timeout_seconds == 60

    @pytest.mark.asyncio
    async def test_decorated_function_still_callable(self) -> None:
        @native_tool(description="Test")
        async def echo(text: str = "", **kwargs: Any) -> str:
            return f"echo: {text}"

        result = await echo(text="hello")
        assert result == "echo: hello"

    def test_no_required_omits_key(self) -> None:
        @native_tool(description="Test")
        async def func(**kw: Any) -> str:
            return ""

        assert "required" not in func.tool.input_schema

    def test_when_to_use_field(self) -> None:
        """R3.2: @native_tool accepts when_to_use."""
        @native_tool(
            description="Send a message",
            when_to_use="When you need to communicate with a teammate",
        )
        async def send(**kw: Any) -> str:
            return ""

        assert send.tool.when_to_use == "When you need to communicate with a teammate"

    def test_example_field(self) -> None:
        """R3.2: @native_tool accepts example."""
        @native_tool(
            description="Read a file",
            example='read_file(path="/etc/hosts")',
        )
        async def read_file(**kw: Any) -> str:
            return ""

        assert read_file.tool.example == 'read_file(path="/etc/hosts")'

    def test_category_field(self) -> None:
        """R3.2: @native_tool accepts category."""
        @native_tool(description="Send a message", category="messaging")
        async def send(**kw: Any) -> str:
            return ""

        assert send.tool.category == "messaging"

    def test_all_three_new_fields(self) -> None:
        """R3.2, R3.3: All three new fields flow to RegisteredTool."""
        @native_tool(
            description="Test tool",
            when_to_use="When testing",
            example="test()",
            category="testing",
        )
        async def test_tool(**kw: Any) -> str:
            return ""

        assert test_tool.tool.when_to_use == "When testing"
        assert test_tool.tool.example == "test()"
        assert test_tool.tool.category == "testing"

    def test_new_fields_default_empty(self) -> None:
        """R3.1, R3.4: New fields default to empty string."""
        @native_tool(description="Minimal tool")
        async def minimal(**kw: Any) -> str:
            return ""

        assert minimal.tool.when_to_use == ""
        assert minimal.tool.example == ""
        assert minimal.tool.category == ""
