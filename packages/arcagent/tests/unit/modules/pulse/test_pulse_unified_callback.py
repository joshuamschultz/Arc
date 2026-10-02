"""Pulse migration to the unified ``run_collected`` callback (SPEC-027).

Verifies the engine invokes ``agent_run_fn`` with ``session_key`` and no
``tool_choice``/``automated`` kwargs, and that the ``agent:ready`` hook binds
the single ``run_fn`` directly without wrapping it.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from arcagent.core.control_contract import SignedControlRevision
from arcagent.core.run_contract import CanonicalRunRequest
from arcagent.modules.pulse import PulseCheck, PulseState, _runtime
from arcagent.modules.pulse.capabilities import bind_agent_run_fn
from arcagent.modules.pulse.config import PulseConfig
from arcagent.modules.pulse.engine import PulseEngine
from arcagent.modules.pulse.signed_dispatch import canonical_definition


class _RunResult:
    """Minimal stand-in for arcrun ``RunResult`` (has ``.content``)."""

    def __init__(self, content: str) -> None:
        self.content = content


class _Recorder:
    """Records calls to the unified ``run_collected`` callback."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, prompt: str, **kwargs: Any) -> _RunResult:
        self.calls.append((prompt, kwargs))
        return _RunResult("done")


@pytest.fixture(autouse=True)
def _clean_runtime() -> Any:
    _runtime.reset()
    yield
    _runtime.reset()


def _engine(workspace: Path, run_fn: Any) -> PulseEngine:
    def prepare(prompt: str, **kwargs: Any) -> CanonicalRunRequest:
        kwargs["purpose"] = kwargs.pop("run_purpose")
        return CanonicalRunRequest(input_text=prompt, **kwargs)

    async def issue(request: CanonicalRunRequest, evidence: bytes) -> tuple[bytes, datetime]:
        del request, evidence
        return b"signed", datetime.now(UTC) + timedelta(minutes=1)

    engine = PulseEngine(
        workspace=workspace,
        config=PulseConfig(timeout_seconds=5.0),
        agent_run_fn=run_fn,
        control_artifact_authority=AsyncMock(),
        control_tenant_id="tenant",
        agent_did="did:arc:local:agent/one",
        trigger_issuer=issue,
        prepare_collected_request=prepare,
    )
    engine.set_agent_run_fn(run_fn)
    return engine


def _approved(check: PulseCheck) -> PulseCheck:
    return check.model_copy(
        update={
            "approval": SignedControlRevision(
                tenant_id="tenant",
                agent_did="did:arc:local:agent/one",
                purpose="pulse",
                artifact_id=check.name,
                revision=1,
                definition_digest=hashlib.sha256(canonical_definition(check)).hexdigest(),
                actor_did="did:arc:local:user/operator",
                issued_at=datetime.now(UTC),
                signature="ab" * 64,
            )
        }
    )


@pytest.mark.asyncio
async def test_execute_check_calls_run_fn_with_session_key(tmp_path: Path) -> None:
    recorder = _Recorder()
    engine = _engine(tmp_path, recorder)
    check = _approved(PulseCheck(name="inbox", interval_minutes=10, action="Check inbox"))

    await engine._execute_check(check, PulseState())

    assert len(recorder.calls) == 1
    prompt, kwargs = recorder.calls[0]
    assert kwargs["session_key"] == "pulse:inbox"
    assert kwargs["run_purpose"] == "pulse"
    assert kwargs["signed_authorization"] == b"signed"
    assert "Check inbox" in prompt


@pytest.mark.asyncio
async def test_execute_check_drops_tool_choice_and_automated(tmp_path: Path) -> None:
    recorder = _Recorder()
    engine = _engine(tmp_path, recorder)
    check = _approved(PulseCheck(name="health", interval_minutes=5, action="Ping"))

    await engine._execute_check(check, PulseState())

    _, kwargs = recorder.calls[0]
    assert "tool_choice" not in kwargs
    assert "automated" not in kwargs


@pytest.mark.asyncio
async def test_session_key_is_per_check(tmp_path: Path) -> None:
    recorder = _Recorder()
    engine = _engine(tmp_path, recorder)

    await engine._execute_check(
        _approved(PulseCheck(name="alpha", interval_minutes=1, action="a")), PulseState()
    )
    await engine._execute_check(
        _approved(PulseCheck(name="beta", interval_minutes=1, action="b")), PulseState()
    )

    keys = [kwargs["session_key"] for _, kwargs in recorder.calls]
    assert keys == ["pulse:alpha", "pulse:beta"]


@pytest.mark.asyncio
async def test_execute_check_marks_ok_after_success(tmp_path: Path) -> None:
    recorder = _Recorder()
    engine = _engine(tmp_path, recorder)
    check = _approved(PulseCheck(name="inbox", interval_minutes=10, action="Check inbox"))

    await engine._execute_check(check, PulseState())

    state = engine._read_state()
    assert state.checks["inbox"].last_result == "ok"


class _Ctx:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data


@pytest.mark.asyncio
async def test_ready_hook_binds_run_fn_directly(tmp_path: Path) -> None:
    """The hook stores the raw ``run_fn`` — no ``automated`` wrapper."""
    _runtime.configure(config=PulseConfig(), workspace=tmp_path)
    recorder = _Recorder()

    await bind_agent_run_fn(_Ctx({"run_fn": recorder}))

    assert _runtime.state().agent_run_fn is recorder


@pytest.mark.asyncio
async def test_ready_hook_unblocks_running_engine(tmp_path: Path) -> None:
    _runtime.configure(config=PulseConfig(), workspace=tmp_path)
    recorder = _Recorder()
    engine = PulseEngine(
        workspace=tmp_path,
        config=PulseConfig(),
        agent_run_fn=recorder,
    )
    _runtime.state().engine = engine

    await bind_agent_run_fn(_Ctx({"run_fn": recorder}))

    assert engine._agent_run_fn is recorder


@pytest.mark.asyncio
async def test_ready_hook_ignores_missing_run_fn(tmp_path: Path) -> None:
    _runtime.configure(config=PulseConfig(), workspace=tmp_path)

    await bind_agent_run_fn(_Ctx({}))

    assert _runtime.state().agent_run_fn is None
