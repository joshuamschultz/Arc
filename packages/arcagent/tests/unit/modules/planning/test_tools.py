"""Unit tests for planning module tools."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arcagent.modules.planning.tools import create_planning_tools


def _find_tool(tools: list, name: str):
    """Find a tool by name from a list of RegisteredTools."""
    for tool in tools:
        if tool.name == name:
            return tool
    msg = f"Tool '{name}' not found"
    raise ValueError(msg)


@pytest.fixture
def tools(tmp_path: Path):
    """Create planning tools against a temp workspace."""
    return create_planning_tools(tmp_path)


class TestToolRegistration:
    def test_four_tools_created(self, tools) -> None:
        assert len(tools) == 4
        names = [t.name for t in tools]
        assert "task_create" in names
        assert "task_list" in names
        assert "task_update" in names
        assert "task_complete" in names

    def test_tools_have_schemas(self, tools) -> None:
        for tool in tools:
            assert tool.input_schema is not None
            assert tool.input_schema.get("type") == "object"

    def test_tools_have_source(self, tools) -> None:
        for tool in tools:
            assert tool.source == "planning"


class TestTaskCreate:
    @pytest.mark.asyncio
    async def test_create_returns_id(self, tools, tmp_path: Path) -> None:
        tool = _find_tool(tools, "task_create")
        result = await tool.execute(description="Do something")
        data = json.loads(result)
        assert "id" in data
        assert data["status"] == "pending"
        assert data["description"] == "Do something"

    @pytest.mark.asyncio
    async def test_create_persists_to_file(self, tools, tmp_path: Path) -> None:
        tool = _find_tool(tools, "task_create")
        await tool.execute(description="Step 1: gather info")
        tasks_file = tmp_path / "tasks.json"
        assert tasks_file.exists()
        tasks = json.loads(tasks_file.read_text(encoding="utf-8"))
        assert len(tasks) == 1
        assert tasks[0]["description"] == "Step 1: gather info"

    @pytest.mark.asyncio
    async def test_create_empty_description_errors(self, tools) -> None:
        tool = _find_tool(tools, "task_create")
        result = await tool.execute(description="")
        data = json.loads(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_create_multiple_unique_ids(self, tools) -> None:
        tool = _find_tool(tools, "task_create")
        r1 = json.loads(await tool.execute(description="Task 1"))
        r2 = json.loads(await tool.execute(description="Task 2"))
        assert r1["id"] != r2["id"]


class TestTaskList:
    @pytest.mark.asyncio
    async def test_list_empty(self, tools) -> None:
        tool = _find_tool(tools, "task_list")
        result = await tool.execute()
        data = json.loads(result)
        assert data["tasks"] == []
        assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_list_returns_created_tasks(self, tools) -> None:
        create = _find_tool(tools, "task_create")
        await create.execute(description="Task A")
        await create.execute(description="Task B")

        list_tool = _find_tool(tools, "task_list")
        result = await list_tool.execute()
        data = json.loads(result)
        assert data["total"] == 2

    @pytest.mark.asyncio
    async def test_list_filter_by_status(self, tools) -> None:
        create = _find_tool(tools, "task_create")
        r1 = json.loads(await create.execute(description="Task A"))
        await create.execute(description="Task B")

        update = _find_tool(tools, "task_update")
        await update.execute(id=r1["id"], status="in_progress")

        list_tool = _find_tool(tools, "task_list")
        result = await list_tool.execute(status="pending")
        data = json.loads(result)
        assert data["total"] == 1
        assert data["tasks"][0]["description"] == "Task B"


class TestTaskUpdate:
    @pytest.mark.asyncio
    async def test_update_status(self, tools) -> None:
        create = _find_tool(tools, "task_create")
        r = json.loads(await create.execute(description="Do work"))

        update = _find_tool(tools, "task_update")
        result = await update.execute(id=r["id"], status="in_progress")
        data = json.loads(result)
        assert data["status"] == "in_progress"

    @pytest.mark.asyncio
    async def test_update_nonexistent_task(self, tools) -> None:
        update = _find_tool(tools, "task_update")
        result = await update.execute(id="nonexistent", status="done")
        data = json.loads(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_update_persists(self, tools, tmp_path: Path) -> None:
        create = _find_tool(tools, "task_create")
        r = json.loads(await create.execute(description="Task"))

        update = _find_tool(tools, "task_update")
        await update.execute(id=r["id"], status="in_progress")

        tasks = json.loads(
            (tmp_path / "tasks.json").read_text(encoding="utf-8"),
        )
        assert tasks[0]["status"] == "in_progress"


class TestTaskComplete:
    @pytest.mark.asyncio
    async def test_complete_marks_done(self, tools) -> None:
        create = _find_tool(tools, "task_create")
        r = json.loads(await create.execute(description="Gather info"))

        complete = _find_tool(tools, "task_complete")
        result = await complete.execute(id=r["id"], result="Info gathered")
        data = json.loads(result)
        assert data["status"] == "done"
        assert data["result"] == "Info gathered"

    @pytest.mark.asyncio
    async def test_complete_nonexistent_errors(self, tools) -> None:
        complete = _find_tool(tools, "task_complete")
        result = await complete.execute(id="nonexistent", result="done")
        data = json.loads(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_no_auto_messaging(self, tools) -> None:
        """Planning tasks don't auto-send to anyone — agent decides."""
        create = _find_tool(tools, "task_create")
        r = json.loads(await create.execute(description="Solo task"))

        complete = _find_tool(tools, "task_complete")
        result = await complete.execute(id=r["id"], result="Done")
        data = json.loads(result)
        assert data["status"] == "done"
        assert "sent_to" not in data
