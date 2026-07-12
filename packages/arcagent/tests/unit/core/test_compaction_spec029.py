"""SPEC-029 Phase 3 — compaction reconciliation (arcagent).

Covers: structured summary schema (D-399), observation masking persisted at the
boundary (D-400), deep/debounced split (D-396/024), append-only transform_context
(D-398), and the compaction audit event (D-030).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from arcagent.core.config import ContextConfig, SessionConfig
from arcagent.core.session_internal.context import ContextManager
from arcagent.core.session_internal.manager import SessionManager


def _model(text: str = "goal: do X\nnext_step: finish") -> MagicMock:
    m = MagicMock()
    m.invoke = AsyncMock(return_value=MagicMock(content=text))
    return m


def _telemetry() -> MagicMock:
    t = MagicMock()
    t.audit_event = MagicMock()
    return t


def _sm(workspace: Path, context_manager: Any) -> SessionManager:
    return SessionManager(
        config=SessionConfig(),
        context_config=ContextConfig(max_tokens=400, estimate_multiplier=1.0),
        telemetry=_telemetry(),
        workspace=workspace,
        context_manager=context_manager,
    )


# --- D-398: transform_context is append-only ------------------------------


class TestTransformContextAppendOnly:
    def _cm(self) -> ContextManager:
        return ContextManager(
            config=ContextConfig(max_tokens=100000, estimate_multiplier=1.0),
            telemetry=_telemetry(),
        )

    def test_prefix_stable_across_appended_turns(self) -> None:
        cm = self._cm()
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
        out1 = cm.transform_context(msgs)
        # simulate a turn appending
        msgs2 = [*msgs, {"role": "assistant", "content": "reply"}]
        out2 = cm.transform_context(msgs2)
        assert out1 == msgs  # identity
        assert out2[: len(out1)] == out1  # prefix preserved, only appended


# --- D-396/024: deep, debounced split -------------------------------------


class TestDeepSplit:
    def test_kept_tail_under_45_percent(self) -> None:
        cm = ContextManager(
            config=ContextConfig(max_tokens=400, estimate_multiplier=1.0),
            telemetry=_telemetry(),
        )
        # 12 messages of ~50 tokens each (~600 tokens total, well over budget)
        msgs = [{"role": "user", "content": "x" * 200} for _ in range(12)]
        older, keep = cm.compaction_split(msgs)
        assert older and keep  # both non-empty
        kept_tokens = sum(cm.estimate_tokens(m["content"]) for m in keep)
        # keep budget is 45% of 400 = 180 tokens → deep compaction, no thrash
        assert kept_tokens <= 180 + 50  # +one message of slack


# --- D-400: masking persisted at the boundary -----------------------------


class TestBoundaryMasking:
    async def test_compact_masks_kept_window_and_persists(self, tmp_path: Path) -> None:
        old_msgs = [{"role": "user", "content": "old"}]
        kept = [{"role": "tool", "content": "y" * 500, "tool_call_id": "tc1"}]
        kept_masked = [
            {"role": "tool", "content": "[output pruned — 60 tokens]", "tool_call_id": "tc1"}
        ]

        cm = MagicMock()
        cm.compaction_split.return_value = (old_msgs, kept)
        cm.prune_observations.return_value = kept_masked

        sm = _sm(tmp_path, cm)
        await sm.create_session()
        for i in range(6):
            await sm.append_message({"role": "user", "content": f"m{i}"})

        await sm.compact(_model())

        cm.prune_observations.assert_called_once()  # masking applied at boundary
        msgs = sm.get_messages()
        assert msgs[0]["type"] == "compaction_summary"
        assert msgs[1:] == kept_masked  # masked window persisted, not re-derived


# --- D-399: structured summary schema -------------------------------------


class TestStructuredSummary:
    async def test_summary_prompt_uses_schema(self, tmp_path: Path) -> None:
        cm = ContextManager(
            config=ContextConfig(max_tokens=400, estimate_multiplier=1.0),
            telemetry=_telemetry(),
        )
        sm = _sm(tmp_path, cm)
        await sm.create_session()
        for i in range(8):
            await sm.append_message({"role": "user", "content": "x" * 200})

        model = _model()
        await sm.compact(model)

        prompts = [c.args[0][0].content for c in model.invoke.call_args_list]
        summary_prompt = next(p for p in prompts if "next_step" in p)
        for field in ("goal", "constraints", "rejected_approaches", "progress"):
            assert field in summary_prompt
        assert "VERBATIM" in summary_prompt


# --- Review fix: compaction summary must reassemble as a Message (arch D-A) --


class TestCompactionReassembly:
    async def test_compacted_session_rebuilds_into_messages(self, tmp_path: Path) -> None:
        """After compaction, history must rebuild via Message(**record) without
        crashing — the summary entry carries role+content (extra keys ignored)."""
        from arcllm.types import Message

        cm = ContextManager(
            config=ContextConfig(max_tokens=400, estimate_multiplier=1.0),
            telemetry=_telemetry(),
        )
        sm = _sm(tmp_path, cm)
        await sm.create_session()
        for i in range(8):
            await sm.append_message({"role": "user", "content": "x" * 200})

        await sm.compact(_model("goal: X\nnext_step: Y"))

        # This is exactly what agent_dispatch does to build the LLM history.
        history = [Message(**m) for m in sm.get_messages()]
        assert history[0].role == "user"
        assert "Summary of" in history[0].content


# --- Review fix: trigger fires off the estimate over current messages --------


class TestContextRatioTrigger:
    def test_context_ratio_reflects_current_messages(self, tmp_path: Path) -> None:
        cm = ContextManager(
            config=ContextConfig(max_tokens=100, estimate_multiplier=1.0),
            telemetry=_telemetry(),
        )
        sm = _sm(tmp_path, cm)
        sm._messages = [{"role": "user", "content": "x" * 200}]  # ~50 tokens / 100
        ratio = sm.context_ratio()
        assert ratio > 0.0  # NOT the always-zero reported-token path
        assert 0.4 < ratio < 0.6


# --- Review fix: emergency truncate always keeps the newest message ----------


class TestEmergencyTruncateKeepsNewest:
    def test_single_oversized_message_is_kept(self) -> None:
        cm = ContextManager(
            config=ContextConfig(max_tokens=10, estimate_multiplier=1.0),
            telemetry=_telemetry(),
        )
        msgs = [{"role": "user", "content": "x" * 400}]  # ~100 tokens >> 10*0.85
        out = cm._emergency_truncate(msgs)
        assert out == msgs  # never returns an empty list


# --- D-030: compaction audit event ----------------------------------------


class TestCompactionAudit:
    async def test_emits_audit_event(self, tmp_path: Path) -> None:
        cm = ContextManager(
            config=ContextConfig(max_tokens=400, estimate_multiplier=1.0),
            telemetry=_telemetry(),
        )
        sm = _sm(tmp_path, cm)
        await sm.create_session()
        for i in range(8):
            await sm.append_message({"role": "user", "content": "x" * 200})

        await sm.compact(_model())

        sm._telemetry.audit_event.assert_called_once()
        name, details = sm._telemetry.audit_event.call_args.args
        assert name == "context.compaction"
        assert details["messages_after"] < details["messages_before"]
