"""SPEC-043 REQ-005 — SessionManager persists loop checkpoints (JSONL)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arcagent.core.config import ContextConfig, SessionConfig
from arcagent.core.session_internal.manager import SessionManager


class _Telemetry:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def audit_event(self, event: str, payload: dict) -> None:
        self.events.append((event, payload))


class _Checkpoint:
    """Minimal LoopCheckpoint stand-in exposing to_record()."""

    def __init__(self, turn: int) -> None:
        self._turn = turn

    def to_record(self) -> dict:
        return {
            "run_id": "run-1",
            "turn_count": self._turn,
            "tokens_used": {"total": 42},
            "tool_names": ["echo"],
        }


async def _mgr(tmp_path: Path) -> SessionManager:
    telem = _Telemetry()
    mgr = SessionManager(SessionConfig(), ContextConfig(), telem, tmp_path)
    await mgr.create_session()
    return mgr


class TestPersistCheckpoint:
    @pytest.mark.asyncio
    async def test_appends_one_jsonl_line_and_audits(self, tmp_path: Path) -> None:
        mgr = await _mgr(tmp_path)
        await mgr.append_message({"role": "user", "content": "hi"})
        await mgr.persist_checkpoint(_Checkpoint(turn=2))

        path = tmp_path / "sessions" / f"{mgr.session_id}.jsonl"
        lines = [json.loads(x) for x in path.read_text().strip().split("\n")]
        checkpoints = [x for x in lines if x.get("type") == "checkpoint"]
        assert len(checkpoints) == 1
        assert checkpoints[0]["turn_count"] == 2
        assert checkpoints[0]["tool_names"] == ["echo"]
        # Audit event fired (REQ-061).
        assert any(e[0] == "loop.checkpoint" for e in mgr._telemetry.events)

    @pytest.mark.asyncio
    async def test_latest_checkpoint_available_after_persist(self, tmp_path: Path) -> None:
        mgr = await _mgr(tmp_path)
        await mgr.persist_checkpoint(_Checkpoint(turn=5))
        latest = mgr.latest_checkpoint()
        assert latest is not None
        assert latest["turn_count"] == 5

    @pytest.mark.asyncio
    async def test_resume_segregates_checkpoint_from_transcript(self, tmp_path: Path) -> None:
        """Checkpoint records never re-enter the model transcript (REQ-003/005)."""
        mgr = await _mgr(tmp_path)
        await mgr.append_message({"role": "user", "content": "hi"})
        await mgr.persist_checkpoint(_Checkpoint(turn=3))
        await mgr.append_message({"role": "assistant", "content": "there"})
        sid = mgr.session_id

        mgr2 = SessionManager(SessionConfig(), ContextConfig(), _Telemetry(), tmp_path)
        messages = await mgr2.resume_session(sid)
        # Transcript holds only the two conversation turns, NOT the checkpoint.
        assert all(m.get("type") != "checkpoint" for m in messages)
        assert len(messages) == 2
        # The checkpoint is still recoverable for resume.
        assert mgr2.latest_checkpoint()["turn_count"] == 3
