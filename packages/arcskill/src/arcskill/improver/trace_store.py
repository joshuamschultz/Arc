"""Trace storage/analysis — the arcskill half of the old ``trace_collector``.

SPEC-044 splits trace collection: the arcagent extension extracts *primitive*
per-tool signals off the bus; this module owns span assembly, JSONL persistence
(monthly rotation + aggregate index), and load. Provider-free — no arcagent import
(the signals arrive as primitives via :class:`~arcskill.improver.improver.ArcSkillImprover`).
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arcskill.improver._util import sanitize_text, scrub_args
from arcskill.improver.models import SkillTrace, ToolCallRecord

_logger = logging.getLogger("arcskill.improver.trace_store")


class TraceStore:
    """Assemble skill-usage spans from primitive signals and persist them as JSONL."""

    def __init__(
        self,
        workspace: Path,
        *,
        session_id: str = "",
        capture_args: bool = False,
        tier: str = "personal",
    ) -> None:
        self._workspace = workspace
        self._session_id = session_id or str(uuid.uuid4())[:8]
        # Federal stays hash-only regardless of the knob (SI-12(2), non-overridable).
        self._capture_args = capture_args and tier != "federal"
        self._active: OrderedDict[tuple[str, str, str], SkillTrace] = OrderedDict()
        self._seen_calls: OrderedDict[tuple[str, str, str], None] = OrderedDict()
        self._usage_counts: dict[str, int] = {}
        self._turn_number = 0

    @property
    def turn_number(self) -> int:
        return self._turn_number

    @property
    def usage_counts(self) -> dict[str, int]:
        return dict(self._usage_counts)

    def reset_count(self, skill_name: str) -> None:
        self._usage_counts[skill_name] = 0

    def observe(
        self,
        *,
        skill_name: str,
        tool_name: str,
        status: str,
        error_type: str | None,
        args: dict[str, Any] | None = None,
        llm_trace_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        call_id: str | None = None,
    ) -> None:
        """Open a span for ``skill_name`` on first sight this turn, then record the call.

        ``llm_trace_id`` (H-041) is the arcllm request/trace id for the LLM call this
        tool step belongs to; it is recorded as span METADATA so the read-time join can
        later resolve the payload from arcllm's store. The body itself is never copied.
        """
        scope = (session_id or self._session_id, run_id or "")
        if call_id:
            identity = (*scope, call_id)
            if identity in self._seen_calls:
                return
            self._seen_calls[identity] = None
            if len(self._seen_calls) > 8192:
                self._seen_calls.popitem(last=False)
        span_key = (*scope, skill_name)
        span = self._active.get(span_key)
        if span is None:
            span = SkillTrace(
                trace_id=str(uuid.uuid4()),
                session_id=scope[0],
                skill_name=skill_name,
                skill_version=0,
                turn_number=self._turn_number,
                started_at=datetime.now(UTC),
            )
            self._active[span_key] = span
            self._usage_counts[skill_name] = self._usage_counts.get(skill_name, 0) + 1
            if len(self._active) > 1024:
                _, expired = self._active.popitem(last=False)
                expired.ended_at = datetime.now(UTC)
                self._persist(expired)
                _logger.warning("Skill trace span expired before turn close")
        else:
            self._active.move_to_end(span_key)
        if llm_trace_id and llm_trace_id not in span.llm_trace_ids:
            span.llm_trace_ids.append(llm_trace_id)
        args_hash = ""
        captured: dict[str, Any] | None = None
        if args is not None:
            scrubbed = scrub_args(args)  # scrub BEFORE hashing and persistence (REQ-117)
            args_hash = hashlib.sha256(
                json.dumps(scrubbed, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()
            if self._capture_args:
                captured = scrubbed
        span.tool_calls.append(
            ToolCallRecord(
                tool_name=tool_name,
                args_hash=args_hash,
                result_status=status,
                duration_ms=0.0,
                error_type=error_type,
                args=captured,
            )
        )

    def close_turn(
        self,
        *,
        outcome: str = "",
        session_id: str | None = None,
        run_id: str | None = None,
        turn_number: int | None = None,
    ) -> None:
        """Close + persist every active span at turn end; advance the turn counter."""
        self._turn_number += 1
        scope = (session_id or self._session_id, run_id or "")
        for key, span in list(self._active.items()):
            if key[:2] != scope:
                continue
            if turn_number is not None:
                span.turn_number = turn_number
            self._finalize(span, outcome)
            self._persist(span)
            del self._active[key]

    def _finalize(self, span: SkillTrace, outcome: str) -> None:
        span.ended_at = datetime.now(UTC)
        error_count = sum(1 for tc in span.tool_calls if tc.result_status == "error")
        if outcome:
            span.task_outcome = outcome
            span.outcome_source = "evaluator"
        elif error_count == 0:
            span.task_outcome = "success"
        elif error_count == len(span.tool_calls):
            span.task_outcome = "failure"
        else:
            span.task_outcome = "partial"
        if span.outcome_source is None:
            span.outcome_source = "heuristic"
        span.task_summary = sanitize_text(span.task_summary, max_length=200)

    def _persist(self, trace: SkillTrace) -> None:
        traces_dir = self._workspace / "skill_traces" / trace.skill_name
        traces_dir.mkdir(parents=True, exist_ok=True)
        month = trace.started_at.strftime("%Y-%m")
        line = json.dumps(trace.to_dict(), default=str) + "\n"
        with (traces_dir / f"traces-{month}.jsonl").open("a", encoding="utf-8") as f:
            f.write(line)
        self._update_index(traces_dir, trace)

    def _update_index(self, traces_dir: Path, trace: SkillTrace) -> None:
        index_path = traces_dir / "index.json"
        index: dict[str, Any] = {}
        if index_path.exists():
            try:
                index = json.loads(index_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                index = {}
        index["total_traces"] = int(index.get("total_traces", 0)) + 1
        if trace.task_outcome == "success":
            index["success_count"] = int(index.get("success_count", 0)) + 1
        elif trace.task_outcome == "failure":
            index["failure_count"] = int(index.get("failure_count", 0)) + 1
        index_path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")

    def load_traces(self, skill_name: str) -> list[SkillTrace]:
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
                    traces.append(SkillTrace.from_dict(json.loads(line)))
                except (json.JSONDecodeError, KeyError, ValueError):
                    _logger.warning("Skipping malformed trace line in %s", trace_file)
        return traces


__all__ = ["TraceStore"]
