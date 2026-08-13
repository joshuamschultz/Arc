"""SPEC-066 REQ-337 — the per-agent capability copy is the ONE load path.

``arc module install`` writes a module's tools and skills to
``<agent_dir>/capabilities/modules/<name>/`` and leaves its runtime at the
deployment root. Until this suite existed, nothing read that copy: the loader
scanned the SHARED deployment root instead, so every agent on the box loaded
identical code, the copy was dead bytes, and the trust gate the copy was
supposed to pass never ran on it.

The cases below are written so that returning to the shared root fails them.
Each one differs the copy from the deployment original and then asks the LIVE
agent what it registered — never what the copy contains.

**The security condition.** The copy sits in a directory the agent can write,
and a ``module:*`` root is VERIFIED, which means its code runs UNCONTAINED (no
import allowlist, no ArcRun isolation — measured at T-972 as the only way 17 of
18 modules register anything at all). Writable plus uncontained is only safe
while a valid signature is mandatory, so a module capability requires one at
EVERY tier — independent of ``require_signature`` (tier-derived) and of
``auto_run_agent_code`` (which admits code the AGENT wrote, and a module is a
distributed artifact with an issuer). :func:`test_an_unsigned_module_copy_is_denied_at_personal_tier_even_with_auto_run`
is the regression gate on that; without it this change is a downgrade.

``scheduler`` is the module under test because it is real: it is bundled from
the source catalog, verified, materialized, and copied by the same four calls
``arc module install`` makes, and it registers four tools with no network.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import arcbundle
import pytest
from arcrun import StreamEvent, TurnEndEvent
from arctrust import ValidatorsConfig, generate_keypair

import arcagent
from arcagent.capabilities.capability_loader import module_capability_root, pin_name_for_path
from arcagent.core.agent import ArcAgent
from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    IdentityConfig,
    LLMConfig,
    ModuleEntry,
    SecurityConfig,
    TelemetryConfig,
)

#: The repository source tree modules are bundled FROM — never
#: ``arcagent.__file__``, which stops carrying them once SPEC-066 lands fully.
_SOURCE_CATALOG = Path(__file__).resolve().parents[2] / "src" / "arcagent" / "modules"

_MODULE = "scheduler"
_ISSUER = "did:arc:module-copy-test-issuer"
_ISSUER_KEYPAIR = generate_keypair()

#: A tool that exists ONLY in one agent's copy. Its presence in a live registry
#: is proof the copy was loaded; its absence is proof the shared original was.
#: Self-contained (it imports its own decorator) so it can be appended to any
#: module's capability file without depending on what that file already imports.
_MARKER = '''

from arcagent.tools._decorator import tool as _marker_tool


@_marker_tool(description="marker", version="1.0.0", classification="read_only")
async def copy_marker() -> str:
    """Present only in the per-agent copy that was edited and re-signed."""
    return "from-the-copy"
'''


@dataclass(frozen=True)
class Deployment:
    """One arc home with one or more agent directories under it."""

    arc_home: Path

    @property
    def modules_root(self) -> Path:
        return self.arc_home / "modules"


def _deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Deployment:
    """Lay out an empty deployment and point ``ARC_CONFIG_DIR`` at it."""
    arc_home = tmp_path / "arc"
    arc_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ARC_CONFIG_DIR", str(arc_home))
    return Deployment(arc_home=arc_home)


def _agent_dir(tmp_path: Path, name: str) -> Path:
    agent_dir = tmp_path / name
    (agent_dir / "workspace").mkdir(parents=True, exist_ok=True)
    return agent_dir


def _install(deployment: Deployment, agent_dir: Path, tmp_path: Path) -> Path:
    """Install ``scheduler`` for one agent exactly as ``arc module install`` does.

    Build a signed bundle from the source catalog, verify it in full, materialize
    it read-only at the deployment root, and copy its capability surface into
    the agent's own capabilities root. Returns the copy directory.
    """
    bundle = arcbundle.build_bundle(
        _SOURCE_CATALOG / _MODULE,
        module=_MODULE,
        version="1.0.0",
        private_key=_ISSUER_KEYPAIR.private_key,
        issuer=_ISSUER,
        out=tmp_path / "bundles" / f"{agent_dir.name}-{_MODULE}",
    )
    verified = arcbundle.verify_bundle(
        bundle, tier="personal", trusted_issuers={_ISSUER: _ISSUER_KEYPAIR.public_key}
    )
    installed = arcbundle.materialize(verified, deployment.modules_root)
    copied: Path = arcbundle.copy_capabilities(installed, agent_dir, module=_MODULE)
    return copied


def _add_marker_to_copy(copy_dir: Path) -> None:
    """Append the marker tool to ONE agent's copy and re-sign it for the issuer.

    Re-signing is what an operator does after editing a capability, and it is
    required: an edited copy without a fresh signature is denied, which is a
    different case (and its own test below).
    """
    artifact = copy_dir / "capabilities.py"
    artifact.write_text(artifact.read_text(encoding="utf-8") + _MARKER, encoding="utf-8")
    arcagent.write_signature(
        artifact,
        artifact.read_bytes(),
        signer_did=_ISSUER,
        private_key=_ISSUER_KEYPAIR.private_key,
    )


def _config(
    agent_dir: Path,
    *,
    tier: str = "personal",
    auto_run_agent_code: bool = False,
) -> ArcAgentConfig:
    """An agent config enabling ``scheduler`` and pinning the bundle issuer's key."""
    return ArcAgentConfig(
        agent=AgentConfig(
            name=f"{agent_dir.name}-agent",
            org="testorg",
            type="executor",
            workspace=str(agent_dir / "workspace"),
        ),
        llm=LLMConfig(model="test/model"),
        identity=IdentityConfig(key_dir=str(agent_dir / "keys")),
        security=SecurityConfig(
            tier=tier,
            operator_key_dir=str(agent_dir / "operator"),
            validators=ValidatorsConfig(
                trusted_keys=(_ISSUER_KEYPAIR.public_key.hex(),),
                auto_run_agent_code=auto_run_agent_code,
            ),
        ),
        telemetry=TelemetryConfig(enabled=True, export_traces=False),
        # Whole seconds in production; one here so a booted scheduler is not
        # sitting on a half-minute timer inside a test.
        modules={_MODULE: ModuleEntry(enabled=True, config={"check_interval_seconds": 1})},
    )


async def _one_token_turn(*args: Any, **kwargs: Any) -> Any:
    """Stand in for ``arcrun.run_stream`` — the only stub in this file."""

    async def _events() -> AsyncIterator[StreamEvent]:
        yield TurnEndEvent(final_text="ok", tool_calls_made=0)

    return _events()


@asynccontextmanager
async def _booted(agent_dir: Path, config: ArcAgentConfig) -> AsyncIterator[ArcAgent]:
    """Start a real agent over ``agent_dir`` and shut it down afterwards."""
    model = MagicMock()
    model.close = AsyncMock()
    stack: list[Any] = [
        patch("arcagent.core.model_manager.load_eval_model", return_value=model),
        patch("arcagent.utils.model_helpers.load_eval_model", return_value=model),
        patch("arcagent.core.agent_dispatch.arcrun.run_stream", side_effect=_one_token_turn),
    ]
    for entered in stack:
        entered.__enter__()
    agent = ArcAgent(config=config, config_path=agent_dir / "arcagent.toml")
    try:
        await agent.startup()
        yield agent
    finally:
        try:
            await agent.shutdown()
        finally:
            for entered in reversed(stack):
                entered.__exit__(None, None, None)


def _registered_tools(agent: ArcAgent) -> set[str]:
    assert agent._tool_registry is not None
    return set(agent._tool_registry.tools)


def _module_scan_roots(agent: ArcAgent) -> list[tuple[str, Path]]:
    """The ``module:*`` roots the live loader actually holds."""
    assert agent._capability_loader is not None
    return [
        (name, path)
        for name, path in agent._capability_loader._scan_roots
        if name.startswith("module:")
    ]


# --------------------------------------------------------------------------
# The copy is what loads
# --------------------------------------------------------------------------


async def test_the_agents_own_copy_is_what_registers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tool added to the COPY reaches the live registry; the original lacks it.

    The copy and the deployment original are deliberately different bytes, so
    this cannot pass by loading either one — only by loading the copy.
    """
    deployment = _deployment(tmp_path, monkeypatch)
    agent_dir = _agent_dir(tmp_path, "solo")
    copy_dir = _install(deployment, agent_dir, tmp_path)
    _add_marker_to_copy(copy_dir)

    original = (deployment.modules_root / _MODULE / "capabilities.py").read_text(encoding="utf-8")
    assert "copy_marker" not in original, "the test edited the deployment original, not the copy"

    async with _booted(agent_dir, _config(agent_dir)) as agent:
        tools = _registered_tools(agent)

    assert "copy_marker" in tools, (
        "the agent's own capability copy was not loaded — the loader is still "
        f"reading the shared deployment root. Registered: {sorted(tools)}"
    )
    assert "schedule_create" in tools, "the module's own tools stopped registering"


async def test_two_agents_drift_independently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One agent's capability change is invisible to the other.

    Per-agent drift is the product goal the copy exists for (SDD D-648: "a shared
    read-only original cannot serve"). With a shared root both agents would see
    the marker; with no copy consumed at all, neither would.
    """
    deployment = _deployment(tmp_path, monkeypatch)
    drifted_dir = _agent_dir(tmp_path, "drifted")
    steady_dir = _agent_dir(tmp_path, "steady")
    drifted_copy = _install(deployment, drifted_dir, tmp_path)
    _install(deployment, steady_dir, tmp_path)
    _add_marker_to_copy(drifted_copy)

    async with _booted(drifted_dir, _config(drifted_dir)) as agent:
        drifted_tools = _registered_tools(agent)
    async with _booted(steady_dir, _config(steady_dir)) as agent:
        steady_tools = _registered_tools(agent)

    assert "copy_marker" in drifted_tools, "the edited agent did not load its own copy"
    assert "copy_marker" not in steady_tools, (
        "one agent's capability edit reached another agent — the copies are not isolated"
    )
    assert {"schedule_create"} <= steady_tools, "the untouched agent lost the module's own tools"


async def test_no_scan_root_points_at_the_shared_deployment_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lifecycle builds ONE module load path, and it is the per-agent copy.

    Asserted over the roots the loader actually holds rather than over a tool
    listing, because a second root scanning the shared original would be
    invisible in a listing while quietly re-introducing the shared load path.
    """
    deployment = _deployment(tmp_path, monkeypatch)
    agent_dir = _agent_dir(tmp_path, "roots")
    _install(deployment, agent_dir, tmp_path)

    async with _booted(agent_dir, _config(agent_dir)) as agent:
        roots = _module_scan_roots(agent)

    assert roots, "no module root reached the loader at all"
    expected = module_capability_root(agent_dir, _MODULE)
    for name, path in roots:
        assert path == expected or path.parent == expected, (
            f"module root {name} points at {path}, not the agent's copy at {expected}"
        )
        assert deployment.modules_root not in path.parents, (
            f"module root {name} still scans the shared deployment root: {path}"
        )


# --------------------------------------------------------------------------
# The security condition that makes scanning a writable root safe
# --------------------------------------------------------------------------


async def test_an_unsigned_module_copy_is_denied_at_personal_tier_even_with_auto_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unsigned module code never runs, at any tier, whatever the toggle says.

    ``auto_run_agent_code`` is the personal-tier opt-in for code the AGENT wrote
    — which is contained when it runs. A module capability is neither: it is a
    distributed artifact with an issuer, and it runs uncontained. If this toggle
    reached it, an agent could write a ``capabilities.py`` into its own copy
    directory and have it imported into the process (ASI05/ASI06).
    """
    deployment = _deployment(tmp_path, monkeypatch)
    agent_dir = _agent_dir(tmp_path, "unsigned")
    copy_dir = _install(deployment, agent_dir, tmp_path)
    arcagent.sidecar_path(copy_dir / "capabilities.py").unlink()

    config = _config(agent_dir, tier="personal", auto_run_agent_code=True)
    async with _booted(agent_dir, config) as agent:
        tools = _registered_tools(agent)

    assert "schedule_create" not in tools, (
        "an UNSIGNED module capability loaded at personal tier because "
        "auto_run_agent_code was on — writable plus uncontained plus unsigned"
    )


async def test_a_module_copy_edited_without_re_signing_is_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent cannot edit its way into the process: tampering breaks the signature."""
    deployment = _deployment(tmp_path, monkeypatch)
    agent_dir = _agent_dir(tmp_path, "tampered")
    copy_dir = _install(deployment, agent_dir, tmp_path)
    artifact = copy_dir / "capabilities.py"
    artifact.write_text(artifact.read_text(encoding="utf-8") + _MARKER, encoding="utf-8")

    async with _booted(agent_dir, _config(agent_dir)) as agent:
        tools = _registered_tools(agent)

    assert "copy_marker" not in tools, "an unsigned edit to the copy was imported"
    assert "schedule_create" not in tools, "the tampered file registered its other tools"


def test_a_copied_capability_pins_under_its_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A copy and its deployment original adjudicate under the SAME TOFU pin name.

    ``arc module install`` approves each artifact under the name derived from its
    path in the deployment root, and the loader now looks the copy up. If the two
    spelled the name differently, every approval an install wrote would miss and
    an enterprise box would gate every module it just installed.
    """
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc"))
    agent_dir = tmp_path / "agent"
    copy_root = module_capability_root(agent_dir, _MODULE)

    assert pin_name_for_path(copy_root / "capabilities.py") == f"{_MODULE}/capabilities"
    assert pin_name_for_path(copy_root / "skills" / "recall" / "SKILL.md") == (
        f"{_MODULE}/skills/recall"
    )
    deployment_artifact = arcagent.module_root() / _MODULE / "capabilities.py"
    assert pin_name_for_path(deployment_artifact) == pin_name_for_path(
        copy_root / "capabilities.py"
    )
