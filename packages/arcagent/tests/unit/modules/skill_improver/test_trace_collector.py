"""Tests for TraceCollector — skill detection, span tracking, JSONL storage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arcagent.core.module_bus import EventContext
from arcagent.core.skill_registry import SkillMeta, SkillRegistry
from arcagent.modules.skill_improver.config import SkillImproverConfig
from arcagent.modules.skill_improver.trace_collector import (
    TraceCollector,
    _hash_args,
    _parse_expected_tools,
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture
def skill_file(tmp_path: Path) -> Path:
    """Create a sample skill file."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    skill_path = skills_dir / "plan-travel.md"
    skill_path.write_text(
        "---\n"
        "name: plan-travel\n"
        "description: Plan business travel\n"
        "---\n\n"
        "## Steps\n"
        "1. Use the read tool to check calendar\n"
        "2. Use the bash tool to fetch flight data\n"
        "3. Use the write tool to save itinerary\n",
        encoding="utf-8",
    )
    return skill_path


@pytest.fixture
def skill_registry(skill_file: Path) -> SkillRegistry:
    registry = SkillRegistry()
    # Manually add a skill
    registry._skills["plan-travel"] = SkillMeta(
        name="plan-travel",
        description="Plan business travel",
        file_path=skill_file,
    )
    return registry


@pytest.fixture
def config() -> SkillImproverConfig:
    return SkillImproverConfig()


@pytest.fixture
def collector(
    skill_registry: SkillRegistry,
    workspace: Path,
    config: SkillImproverConfig,
) -> TraceCollector:
    return TraceCollector(
        skill_registry=skill_registry,
        workspace=workspace,
        config=config,
        session_id="test-session",
    )


def _make_ctx(event: str = "agent:post_tool", **data: object) -> EventContext:
    return EventContext(
        event=event,
        data=dict(data),
        agent_did="did:test:agent",
        trace_id="trace-test",
    )


class TestHashArgs:
    """Arg hashing for privacy."""

    def test_deterministic(self) -> None:
        assert _hash_args({"a": 1}) == _hash_args({"a": 1})

    def test_different_args(self) -> None:
        assert _hash_args({"a": 1}) != _hash_args({"a": 2})

    def test_sha256_length(self) -> None:
        assert len(_hash_args({})) == 64


class TestParseExpectedTools:
    """Expected tool extraction from skill markdown."""

    def test_extracts_tools(self) -> None:
        text = "Use the read tool, then bash tool, and write tool"
        tools = _parse_expected_tools(text)
        assert tools == ["read", "bash", "write"]

    def test_deduplicates(self) -> None:
        text = "read tool then read tool again"
        tools = _parse_expected_tools(text)
        assert tools == ["read"]

    def test_empty_on_no_match(self) -> None:
        text = "This has zero references to anything"
        tools = _parse_expected_tools(text)
        assert tools == []


class TestSkillPathIndexing:
    """B1: Maps SkillRegistry paths to names."""

    def test_skill_paths_indexed(
        self,
        collector: TraceCollector,
        skill_file: Path,
    ) -> None:
        resolved = skill_file.resolve()
        assert resolved in collector._skill_paths
        assert collector._skill_paths[resolved] == "plan-travel"

    def test_expected_tools_parsed(self, collector: TraceCollector) -> None:
        tools = collector._expected_tools.get("plan-travel", [])
        assert "read" in tools
        assert "bash" in tools
        assert "write" in tools

    def test_reindex_clears_old(
        self,
        collector: TraceCollector,
        skill_registry: SkillRegistry,
    ) -> None:
        collector.index_skills(skill_registry)
        assert len(collector._skill_paths) == 1


class TestSkillReadDetection:
    """B2: Read tool + matching path = span opens."""

    @pytest.mark.asyncio
    async def test_skill_read_opens_span(
        self,
        collector: TraceCollector,
        skill_file: Path,
    ) -> None:
        ctx = _make_ctx(
            tool="read",
            args={"file_path": str(skill_file)},
        )
        await collector.on_post_tool(ctx)
        assert collector.active_span is not None
        assert collector.active_span.skill_name == "plan-travel"

    @pytest.mark.asyncio
    async def test_non_skill_read_no_span(
        self,
        collector: TraceCollector,
        tmp_path: Path,
    ) -> None:
        other_file = tmp_path / "other.py"
        other_file.write_text("print('hello')")
        ctx = _make_ctx(
            tool="read",
            args={"file_path": str(other_file)},
        )
        await collector.on_post_tool(ctx)
        assert collector.active_span is None

    @pytest.mark.asyncio
    async def test_non_read_tool_no_span(self, collector: TraceCollector) -> None:
        ctx = _make_ctx(tool="bash", args={"command": "ls"})
        await collector.on_post_tool(ctx)
        assert collector.active_span is None


class TestToolCallRecording:
    """B3: Tool calls recorded within active spans."""

    @pytest.mark.asyncio
    async def test_tool_calls_captured(
        self,
        collector: TraceCollector,
        skill_file: Path,
    ) -> None:
        # Open span
        ctx = _make_ctx(tool="read", args={"file_path": str(skill_file)})
        await collector.on_post_tool(ctx)

        # Record a tool call
        ctx2 = _make_ctx(
            tool="bash",
            args={"command": "echo hello"},
            result="hello",
            duration=0.05,
        )
        await collector.on_post_tool(ctx2)

        assert len(collector.active_span.tool_calls) == 1  # type: ignore[union-attr]
        tc = collector.active_span.tool_calls[0]  # type: ignore[union-attr]
        assert tc.tool_name == "bash"
        assert tc.result_status == "ok"
        assert tc.duration_ms == 50.0

    @pytest.mark.asyncio
    async def test_no_recording_without_span(
        self,
        collector: TraceCollector,
    ) -> None:
        ctx = _make_ctx(tool="bash", args={"command": "ls"}, duration=0.01)
        await collector.on_post_tool(ctx)
        # No span open, nothing should happen
        assert collector.active_span is None


class TestSpanClose:
    """B4: Span close at turn end (post_plan)."""

    @pytest.mark.asyncio
    async def test_span_closed_on_post_plan(
        self,
        collector: TraceCollector,
        skill_file: Path,
    ) -> None:
        # Open span
        ctx = _make_ctx(tool="read", args={"file_path": str(skill_file)})
        await collector.on_post_tool(ctx)
        assert collector.active_span is not None

        # Close on turn end
        close_ctx = _make_ctx(event="agent:post_plan")
        await collector.on_post_plan(close_ctx)
        assert collector.active_span is None

    @pytest.mark.asyncio
    async def test_close_without_span_is_safe(
        self,
        collector: TraceCollector,
    ) -> None:
        close_ctx = _make_ctx(event="agent:post_plan")
        await collector.on_post_plan(close_ctx)
        assert collector.active_span is None


class TestUsageCounting:
    """B5: Per-skill use count tracking."""

    @pytest.mark.asyncio
    async def test_usage_count_increments(
        self,
        collector: TraceCollector,
        skill_file: Path,
    ) -> None:
        for _ in range(3):
            ctx = _make_ctx(tool="read", args={"file_path": str(skill_file)})
            await collector.on_post_tool(ctx)
            close = _make_ctx(event="agent:post_plan")
            await collector.on_post_plan(close)

        assert collector.usage_counts["plan-travel"] == 3

    def test_reset_count(self, collector: TraceCollector) -> None:
        collector._usage_counts["plan-travel"] = 10
        collector.reset_count("plan-travel")
        assert collector.usage_counts["plan-travel"] == 0


class TestJSONLStorage:
    """B6: Write trace, read traces, monthly rotation."""

    @pytest.mark.asyncio
    async def test_trace_persisted_to_jsonl(
        self,
        collector: TraceCollector,
        skill_file: Path,
        workspace: Path,
    ) -> None:
        # Open and close span
        ctx = _make_ctx(tool="read", args={"file_path": str(skill_file)})
        await collector.on_post_tool(ctx)
        close = _make_ctx(event="agent:post_plan")
        await collector.on_post_plan(close)

        # Verify JSONL file exists
        traces_dir = workspace / "skill_traces" / "plan-travel"
        jsonl_files = list(traces_dir.glob("traces-*.jsonl"))
        assert len(jsonl_files) == 1

        # Verify content is valid JSON
        content = jsonl_files[0].read_text()
        data = json.loads(content.strip())
        assert data["skill_name"] == "plan-travel"

    @pytest.mark.asyncio
    async def test_index_updated(
        self,
        collector: TraceCollector,
        skill_file: Path,
        workspace: Path,
    ) -> None:
        ctx = _make_ctx(tool="read", args={"file_path": str(skill_file)})
        await collector.on_post_tool(ctx)
        close = _make_ctx(event="agent:post_plan")
        await collector.on_post_plan(close)

        index_path = workspace / "skill_traces" / "plan-travel" / "index.json"
        assert index_path.exists()
        index = json.loads(index_path.read_text())
        assert index["total_traces"] == 1

    def test_load_traces_empty(self, collector: TraceCollector) -> None:
        traces = collector.load_traces("nonexistent")
        assert traces == []

    @pytest.mark.asyncio
    async def test_load_traces_round_trip(
        self,
        collector: TraceCollector,
        skill_file: Path,
    ) -> None:
        # Create two traces
        for _ in range(2):
            ctx = _make_ctx(tool="read", args={"file_path": str(skill_file)})
            await collector.on_post_tool(ctx)
            close = _make_ctx(event="agent:post_plan")
            await collector.on_post_plan(close)

        traces = collector.load_traces("plan-travel")
        assert len(traces) == 2
        assert all(t.skill_name == "plan-travel" for t in traces)


class TestTraceSanitization:
    """B7: Args hashed, task truncated to 200 chars."""

    @pytest.mark.asyncio
    async def test_args_hashed_not_stored_raw(
        self,
        collector: TraceCollector,
        skill_file: Path,
    ) -> None:
        ctx = _make_ctx(tool="read", args={"file_path": str(skill_file)})
        await collector.on_post_tool(ctx)

        # Record a tool with secret args
        ctx2 = _make_ctx(
            tool="bash",
            args={"command": "SECRET_TOKEN=abc123"},
            duration=0.01,
        )
        await collector.on_post_tool(ctx2)

        close = _make_ctx(event="agent:post_plan")
        await collector.on_post_plan(close)

        traces = collector.load_traces("plan-travel")
        assert len(traces) == 1
        tc = traces[0].tool_calls[0]
        # Args should be hashed, not raw
        assert "SECRET_TOKEN" not in tc.args_hash
        assert len(tc.args_hash) == 64  # SHA-256


class TestCoverage:
    """Coverage metric computation."""

    @pytest.mark.asyncio
    async def test_coverage_computed_on_close(
        self,
        collector: TraceCollector,
        skill_file: Path,
    ) -> None:
        # Open span (expected: read, bash, write)
        ctx = _make_ctx(tool="read", args={"file_path": str(skill_file)})
        await collector.on_post_tool(ctx)

        # Only use bash (1 of 3 expected tools)
        ctx2 = _make_ctx(tool="bash", args={"command": "ls"}, duration=0.01)
        await collector.on_post_tool(ctx2)

        close = _make_ctx(event="agent:post_plan")
        await collector.on_post_plan(close)

        traces = collector.load_traces("plan-travel")
        assert len(traces) == 1
        # bash is 1 of 3 expected tools
        assert traces[0].coverage_pct == pytest.approx(33.3, abs=0.1)
