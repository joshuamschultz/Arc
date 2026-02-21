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
