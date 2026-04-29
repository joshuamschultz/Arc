"""Trace collector — passive skill execution monitoring.

Detects when an agent reads a skill file (via SkillRegistry path matching),
opens a trace span, records tool calls within the span, and stores
completed traces as JSONL for later optimization.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arcagent.core.module_bus import EventContext
from arcagent.modules.skill_improver.config import SkillImproverConfig
from arcagent.modules.skill_improver.models import SkillTrace, ToolCallRecord
from arcagent.utils.sanitizer import sanitize_text

_logger = logging.getLogger("arcagent.modules.skill_improver.trace_collector")

# Regex for extracting expected tools from skill markdown
_TOOL_PATTERN = re.compile(r"(?:^|\s)(\w+)\s+tool\b", re.IGNORECASE)


def _hash_args(args: dict[str, Any]) -> str:
    """SHA-256 hash of tool args for privacy (never store raw args)."""
    serialized = json.dumps(args, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _parse_expected_tools(skill_text: str) -> list[str]:
    """Extract expected tool names from skill markdown at discovery time.

    Looks for patterns like 'read tool', 'bash tool', etc.
    """
    matches = _TOOL_PATTERN.findall(skill_text)
    # Deduplicate preserving order
    seen: set[str] = set()
    result: list[str] = []
    for m in matches:
        lower = m.lower()
        if lower not in seen:
            seen.add(lower)
            result.append(lower)
    return result


class TraceCollector:
    """Passive skill execution trace collector.

    Detects skill reads via path matching against SkillRegistry,
    captures tool calls within active spans, and writes completed
    traces to JSONL storage.
    """

    def __init__(
        self,
        skill_registry: Any,
        workspace: Path,
        config: SkillImproverConfig,
        session_id: str = "",
    ) -> None:
        self._skill_paths: dict[Path, str] = {}  # resolved path -> skill name
        self._expected_tools: dict[str, list[str]] = {}  # skill_name -> tools
        self._active_span: SkillTrace | None = None
        self._usage_counts: dict[str, int] = {}
        self._workspace = workspace
        self._config = config
        self._session_id = session_id or str(uuid.uuid4())[:8]
        self._turn_number = 0

        self.index_skills(skill_registry)

    @property
    def turn_number(self) -> int:
        """Current turn number (read-only)."""
        return self._turn_number

    @property
    def usage_counts(self) -> dict[str, int]:
        """Per-skill usage counts since last reset."""
        return dict(self._usage_counts)

    @property
    def active_span(self) -> SkillTrace | None:
        """Currently active trace span, if any."""
        return self._active_span

    def index_skills(self, skill_registry: Any) -> None:
        """Build path -> name lookup from SkillRegistry."""
        self._skill_paths.clear()
        for skill in skill_registry.skills:
            resolved = skill.file_path.resolve()
            self._skill_paths[resolved] = skill.name
            # Parse expected tools from skill content
            try:
                text = skill.file_path.read_text(encoding="utf-8")
                self._expected_tools[skill.name] = _parse_expected_tools(text)
            except OSError:
                self._expected_tools[skill.name] = []

    def reset_count(self, skill_name: str) -> None:
        """Reset usage count for a specific skill."""
        self._usage_counts[skill_name] = 0

    async def on_post_tool(self, ctx: EventContext) -> None:
        """Priority 200. Detect skill reads, capture tool calls."""
        tool = ctx.data.get("tool", "")

        if tool == "read":
            file_path = ctx.data.get("args", {}).get("file_path", "")
            if file_path:
                try:
                    resolved = Path(file_path).resolve()
                except (ValueError, OSError):
                    return
                if resolved in self._skill_paths:
                    self._close_span()
                    self._open_span(self._skill_paths[resolved], ctx)
                    return

        # Record tool calls within active span
        if self._active_span is not None and tool:
            self._record_tool_call(ctx)

    async def on_post_plan(self, ctx: EventContext) -> None:
        """Priority 200. Close span at turn end."""
        self._turn_number += 1
        self._close_span()

    def _open_span(self, skill_name: str, ctx: EventContext) -> None:
        """Start a new trace span for a skill usage."""
        self._active_span = SkillTrace(
            trace_id=str(uuid.uuid4()),
            session_id=self._session_id,
            skill_name=skill_name,
            skill_version=0,
            turn_number=self._turn_number,
            started_at=datetime.now(UTC),
            expected_tools=self._expected_tools.get(skill_name, []),
        )
        self._usage_counts[skill_name] = self._usage_counts.get(skill_name, 0) + 1

    def _record_tool_call(self, ctx: EventContext) -> None:
        """Record a tool call within the active span."""
        if self._active_span is None:
            return

        tool_name = ctx.data.get("tool", "")
        args = ctx.data.get("args", {})
        result = ctx.data.get("result")
        duration = ctx.data.get("duration", 0.0)

        # Determine status from context
        error_type: str | None = None
        if ctx.is_vetoed:
            status = "vetoed"
        elif isinstance(result, Exception):
            status = "error"
            error_type = type(result).__name__
        else:
            status = "ok"

        record = ToolCallRecord(
            tool_name=tool_name,
            args_hash=_hash_args(args),
            result_status=status,
            duration_ms=float(duration) * 1000,
            error_type=error_type,
        )
        self._active_span.tool_calls.append(record)

    def _close_span(self) -> None:
        """Close the active span and persist the trace."""
        if self._active_span is None:
            return

        span = self._active_span
        self._active_span = None
        span.ended_at = datetime.now(UTC)

        # Compute coverage (actual vs expected tools)
        if span.expected_tools:
            actual_tools = {tc.tool_name for tc in span.tool_calls}
            expected_set = set(span.expected_tools)
            covered = actual_tools & expected_set
            span.coverage_pct = (len(covered) / len(expected_set)) * 100.0
        else:
            span.coverage_pct = 100.0  # No expectations = fully covered

        # Heuristic outcome
        error_count = sum(1 for tc in span.tool_calls if tc.result_status == "error")
        if error_count == 0:
            span.task_outcome = "success"
        elif error_count == len(span.tool_calls):
            span.task_outcome = "failure"
        else:
            span.task_outcome = "partial"
        span.outcome_source = "heuristic"

        # Sanitize and truncate task summary for privacy (ASI-06 defense)
        span.task_summary = sanitize_text(span.task_summary, max_length=200)

        self._persist_trace(span)

    def _persist_trace(self, trace: SkillTrace) -> None:
        """Write trace to JSONL file with monthly rotation."""
        traces_dir = self._workspace / "skill_traces" / trace.skill_name
        traces_dir.mkdir(parents=True, exist_ok=True)

        month = trace.started_at.strftime("%Y-%m")
        trace_file = traces_dir / f"traces-{month}.jsonl"

        line = json.dumps(trace.to_dict(), default=str) + "\n"
        with trace_file.open("a", encoding="utf-8") as f:
            f.write(line)

        self._update_index(traces_dir, trace)

    def _update_index(self, traces_dir: Path, trace: SkillTrace) -> None:
        """Update aggregate index file."""
        index_path = traces_dir / "index.json"

        if index_path.exists():
            try:
                index: dict[str, Any] = json.loads(
                    index_path.read_text(encoding="utf-8"),
                )
            except (json.JSONDecodeError, OSError):
                index = {}
        else:
            index = {}

        total = int(index.get("total_traces", 0)) + 1
        success = int(index.get("success_count", 0))
        failure = int(index.get("failure_count", 0))

        if trace.task_outcome == "success":
            success += 1
        elif trace.task_outcome == "failure":
            failure += 1

        index.update(
            {
                "total_traces": total,
                "success_count": success,
                "failure_count": failure,
            }
        )

        index_path.write_text(
            json.dumps(index, indent=2) + "\n",
            encoding="utf-8",
        )

    def load_traces(self, skill_name: str) -> list[SkillTrace]:
        """Load all traces for a skill from JSONL storage."""
        traces_dir = self._workspace / "skill_traces" / skill_name
        if not traces_dir.exists():
            return []

        traces: list[SkillTrace] = []
        for trace_file in sorted(traces_dir.glob("traces-*.jsonl")):
            try:
                text = trace_file.read_text(encoding="utf-8")
            except OSError:
                continue
            for line in text.strip().splitlines():
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                    traces.append(SkillTrace.from_dict(data))
                except (json.JSONDecodeError, KeyError, ValueError):
                    _logger.warning("Skipping malformed trace line in %s", trace_file)
        return traces
