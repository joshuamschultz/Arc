"""E2E: the workpad hook is actually WIRED to the real bus.

Unit tests prove ``track_runs`` works when called directly. This proves the
activating wiring: a real ``ArcAgent`` with ``[modules.workpad]`` enabled
registers the hook, subscribes it to the bus, and a real ``agent:post_respond``
emission drives the every-N-runs rewrite of ``context.md``. Only the eval model
is stubbed (external dependency).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import arcbundle
import pytest
from arctrust import ValidatorsConfig, generate_keypair
from arctrust.paths import module_root

import arcagent
from arcagent.core import turn_context
from arcagent.core.agent import ArcAgent
from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    ContextConfig,
    IdentityConfig,
    LLMConfig,
    ModuleEntry,
    SecurityConfig,
    TelemetryConfig,
)
from arcagent.modules.workpad import _runtime

_ISSUER = "did:arc:workpad-e2e-operator"
_ISSUER_KEYPAIR = generate_keypair()


def _config(tmp_path: Path, workspace: Path) -> ArcAgentConfig:
    return ArcAgentConfig(
        agent=AgentConfig(
            name="workpad-agent", org="testorg", type="executor", workspace=str(workspace)
        ),
        llm=LLMConfig(model="test/model"),
        identity=IdentityConfig(did="", key_dir=str(tmp_path / "keys"), vault_path=""),
        telemetry=TelemetryConfig(enabled=True),
        context=ContextConfig(max_tokens=10000),
        # The bundle issuer's key, pinned as ``arc module install`` pins it. A
        # ``module:*`` root is VERIFIED, so an unpinned issuer means the module
        # materializes and then registers nothing.
        security=SecurityConfig(
            validators=ValidatorsConfig(trusted_keys=(_ISSUER_KEYPAIR.public_key.hex(),))
        ),
        modules={"workpad": ModuleEntry(enabled=True, config={"every_n_runs": 2})},
    )


def _post_respond_event() -> dict[str, Any]:
    return {
        "result": None,
        "messages": [
            {"role": "user", "content": "pay the vendor invoice by Friday"},
            {"role": "assistant", "content": "noted — I'll track that"},
        ],
        "session_id": "s1",
        "automated": False,
    }


def _install_workpad(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Install the workpad module for this test's agent, the way an operator does.

    SPEC-066 REQ-333/337: discovery reads :func:`arctrust.paths.module_root`, so
    enabling ``[modules.workpad]`` only loads a module an operator installed, and
    the module's TOOLS are read from the per-agent copy at
    ``<agent_dir>/capabilities/modules/workpad/`` rather than from the shared
    deployment tree. All four calls ``arc module install`` makes run here. Built
    and verified rather than copied by hand, because a hand-copied tree carries
    no ``.arcsig`` sidecars and a ``module:*`` root is adjudicated by signature —
    it would load nothing and prove nothing.
    """
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc"))
    bundle = arcbundle.build_bundle(
        Path(arcagent.__file__).resolve().parent / "modules" / "workpad",
        module="workpad",
        version="1.0.0",
        private_key=_ISSUER_KEYPAIR.private_key,
        issuer=_ISSUER,
        out=tmp_path / "bundles" / "workpad.arcbundle",
    )
    verified = arcbundle.verify_bundle(
        bundle, tier="personal", trusted_issuers={_ISSUER: _ISSUER_KEYPAIR.public_key}
    )
    installed = arcbundle.materialize(verified, module_root(tmp_path / "arc"))
    arcbundle.copy_capabilities(installed, tmp_path, module="workpad")


@pytest.mark.asyncio
async def test_post_respond_drives_context_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_workpad(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # ``config_path`` is explicit because the agent's capability roots hang off
    # its parent; the default (relative ``arcagent.toml``) would resolve them
    # against the process CWD and miss the module copy installed above.
    agent = ArcAgent(config=_config(tmp_path, workspace), config_path=tmp_path / "arcagent.toml")
    await agent.startup()

    # Stub the eval model on the module's live runtime state (external dep only).
    model = MagicMock()
    model.invoke = AsyncMock(
        return_value=SimpleNamespace(
            content="Updated: 2026-07-12 | WAITING ON: 1\n\n## WAITING ON\n- vendor invoice (Fri)"
        )
    )
    _runtime.state().eval_model = model

    # These emissions stand in for real, person-driven turns, so mark the turn
    # interactive — the workpad cadence only counts turns a person drove, never
    # background self-wakes (ADR D-726). Without this the runs never count and the
    # rewrite never fires.
    turn_context.set_interactive(True)

    # Two real bus emissions → every_n_runs=2 fires the rewrite on the 2nd.
    assert agent._bus is not None
    await agent._bus.emit("agent:post_respond", _post_respond_event())
    assert not (workspace / "context.md").exists()  # not yet
    await agent._bus.emit("agent:post_respond", _post_respond_event())

    await asyncio.gather(*list(_runtime.state().background_tasks), return_exceptions=True)

    context = (workspace / "context.md").read_text(encoding="utf-8")
    assert "## WAITING ON" in context
    assert "vendor invoice" in context
    model.invoke.assert_awaited_once()
