"""workpad_update — the agent's mediated path to its own context.md.

``context.md`` is a protected name: no LLM-facing file tool may write it
(ASI-06 — a prompt injection must not rewrite standing working memory). The
workpad module is its sole writer. This tool closes the agency gap that guard
created: the agent asks for an immediate curated rewrite, passing notes; the
same sanitized, size-capped maintainer produces the file.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import arcrun
import pytest

from arcagent.core.config import EvalConfig
from arcagent.modules.workpad import _runtime, capabilities
from arcagent.modules.workpad.config import WorkpadConfig


def _state(tmp_path: Path) -> _runtime._State:
    return _runtime._State(
        config=WorkpadConfig(),
        eval_config=EvalConfig(),
        workspace=tmp_path,
        telemetry=None,
        llm_config=None,
        eval_label="test",
        agent_did="did:arc:test:workpad",
        eval_model=object(),
    )


def test_update_runs_the_maintainer_now_with_the_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    st = _state(tmp_path)
    st.transcript = ["[user] earlier turn"]
    monkeypatch.setattr(_runtime, "state", lambda: st)
    monkeypatch.setattr(capabilities, "_eval_model", lambda: st.eval_model)
    seen: dict[str, Any] = {}

    async def fake_oneshot(model: Any, *, system: str, user: str, max_tokens: Any) -> Any:
        seen["user"] = user
        return SimpleNamespace(content="# Context\n\nOpen loops: five projects.\n")

    monkeypatch.setattr(arcrun, "run_oneshot", fake_oneshot)

    result = asyncio.run(
        capabilities.workpad_update(notes="Drop the four closed projects; keep five open.")
    )

    assert "rewritten" in result
    assert "Drop the four closed projects" in seen["user"]
    assert "earlier turn" in seen["user"]
    assert (tmp_path / "context.md").read_text(encoding="utf-8").startswith("# Context")
    # The window drained: the same activity is not replayed at the next cadence.
    assert st.transcript == []


def test_update_reports_when_no_maintainer_model_is_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    st = _state(tmp_path)
    monkeypatch.setattr(_runtime, "state", lambda: st)
    monkeypatch.setattr(capabilities, "_eval_model", lambda: None)

    result = asyncio.run(capabilities.workpad_update(notes="anything"))

    assert "unavailable" in result
    assert not (tmp_path / "context.md").exists()
