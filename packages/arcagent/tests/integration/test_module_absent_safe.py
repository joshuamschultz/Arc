"""Every module can be absent and an agent still completes a turn.

SPEC-066 T-966 (REQ-339). Phase 4 takes all eighteen modules out of the wheel
one at a time; from then on a module is present only because an operator
installed a signed bundle. Absence therefore stops being a config typo and
becomes the ordinary state of a fresh box, an air-gapped install, or a module
whose bundle failed verification. "The agent still runs a turn" is the whole
safety net under that migration, so it has to be measured per module rather
than asserted once.

Parametrization comes from :func:`discover_modules` itself, not a written-out
list of the eighteen names — a module added after this file is written is
covered without anyone remembering to edit it.

**Only the LLM is stubbed.** The agent is a real :class:`ArcAgent`: real
identity, real capability scan, real module runtimes, real bus, real turn
dispatch. A module that breaks agent construction, the capability scan, or the
turn shows up here as a failure attributable to one name.

Absence is injected by ``ARC_CONFIG_DIR`` alone — nothing here patches the
discovery seam. Before T-964 that was impossible, so this file carried a
``_bind_module_root`` helper that pre-bound ``modules_dir`` on
:mod:`arcagent.core.agent_lifecycle`; T-964 moved the scan root to
:func:`arctrust.paths.module_root` and the helper was deleted with it. The
difference matters: every case below now reaches the module root the way a
real deployment does, so a discovery path that only worked under a
monkeypatched seam shows up here as a failure instead of hiding behind one.

**Known blind spot until T-970.** "Absent" here means the module is not
discovered, so it contributes no scan root and no runtime — which is exactly
what T-964/T-965 will make true for real. It does *not* yet mean the module's
Python package is unimportable, because the wheel still ships
``arcagent/modules/``. A hard ``import arcagent.modules.<name>`` from outside
that module therefore still resolves and this suite cannot see it. T-970
removes the source from the wheel; that is the point at which these cases
start covering the cross-import failure mode too, and the point at which any
new red here is a genuine coupling bug rather than a fixture artifact.
"""

from __future__ import annotations

import atexit
import shutil
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from arcrun import StreamEvent, TurnEndEvent
from arctrust.paths import module_root

from arcagent.core.agent import ArcAgent
from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    IdentityConfig,
    LLMConfig,
    ModuleEntry,
    TelemetryConfig,
)
from arcagent.core.module_discovery import discover_modules

# The catalog the module trees are copied FROM: the repository source tree.
# Resolved from the repo layout rather than from ``arcagent.__file__`` because
# T-970 takes the module source out of the wheel, and this suite is the gate
# each module must pass on its way out — it has to keep enumerating real
# modules at the exact moment the installed package stops carrying them.
_SOURCE_CATALOG = Path(__file__).resolve().parents[2] / "src" / "arcagent" / "modules"

# A deployment root staged once at import, and the source of the parametrization
# below. Discovery still decides the set — over a real module root, in the same
# shape production reads — so a module added later is covered without editing
# this file, and a hardcoded list of names never creeps in. Import time, not a
# fixture, because pytest resolves parametrization during collection, before any
# ``tmp_path`` exists.
_STAGED_ROOT = Path(tempfile.mkdtemp(prefix="arc-absent-safe-")) / "modules"
shutil.copytree(_SOURCE_CATALOG, _STAGED_ROOT)
atexit.register(shutil.rmtree, _STAGED_ROOT.parent, True)

_MODULE_NAMES: list[str] = discover_modules(_STAGED_ROOT)


def _arc_home(tmp_path: Path) -> Path:
    """The deployment home this case runs against — what ``ARC_CONFIG_DIR`` names."""
    return tmp_path / "arc"


def _deployment_root(tmp_path: Path, absent: str | None) -> Path:
    """Build a module root holding every discovered module except ``absent``.

    Placed through :func:`arctrust.paths.module_root` rather than composed by
    hand, so a case stages its modules where a real install puts them and where
    discovery reads them — the two cannot drift apart here.

    Copies rather than symlinks: the tree a materialized bundle leaves behind
    is real files, and T-965 loads each ``_runtime.py`` by filesystem path.
    """
    root = module_root(_arc_home(tmp_path))
    root.mkdir(parents=True)
    for name in _MODULE_NAMES:
        if name == absent:
            continue
        shutil.copytree(_SOURCE_CATALOG / name, root / name)
    return root


def _capability_copies(tmp_path: Path, absent: str | None) -> None:
    """Give the agent its per-agent copy of every present module's capabilities.

    An install is two placements, not one (REQ-337): the runtime stays at the
    deployment root and the capability surface is copied to
    ``<agent_dir>/capabilities/modules/<name>/``, which is where the loader reads
    a module's tools and skills from. Presence therefore means both, and this
    function is the second half — without it a "present" module would contribute
    a runtime and no capability root, which is not a state any install produces.

    Unsigned on purpose: this suite measures which modules reach the loader and
    whether a turn completes, never which capabilities clear the trust gate
    (``test_module_capability_trust.py`` owns that).
    """
    for name in _MODULE_NAMES:
        if name == absent:
            continue
        source = _SOURCE_CATALOG / name / "capabilities.py"
        if not source.is_file():
            continue
        dest = tmp_path / "capabilities" / "modules" / name
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest / "capabilities.py")


def _agent_config(tmp_path: Path) -> ArcAgentConfig:
    """A config that enables every discovered module.

    Enabling all of them keeps presence the single variable: whatever the
    parametrized case removes is the only difference between two runs.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return ArcAgentConfig(
        agent=AgentConfig(
            name="absent-safe-agent",
            org="testorg",
            type="executor",
            workspace=str(workspace),
        ),
        llm=LLMConfig(model="test/model"),
        identity=IdentityConfig(key_dir=str(tmp_path / "keys")),
        telemetry=TelemetryConfig(enabled=False),
        modules={name: ModuleEntry(enabled=True) for name in _MODULE_NAMES},
    )


async def _one_token_turn(*args: Any, **kwargs: Any) -> Any:
    """Stand in for ``arcrun.run_stream`` — the only stub in this file."""

    async def _events() -> AsyncIterator[StreamEvent]:
        yield TurnEndEvent(final_text="absent-safe", tool_calls_made=0)

    return _events()


async def _run_a_turn(
    config: ArcAgentConfig, tmp_path: Path
) -> tuple[ArcAgent, list[StreamEvent]]:
    """Start a real agent, run one turn end to end, shut it down.

    ``config_path`` is explicit because the agent's capability roots hang off
    its parent; the default (``arcagent.toml``, relative) would resolve them
    against the process CWD and scan the developer's checkout.
    """
    model = MagicMock()
    model.close = AsyncMock()
    agent = ArcAgent(config=config, config_path=tmp_path / "arcagent.toml")
    with (
        patch("arcagent.core.model_manager.load_eval_model", return_value=model),
        patch("arcagent.utils.model_helpers.load_eval_model", return_value=model),
    ):
        await agent.startup()
        with patch("arcagent.core.agent_dispatch.arcrun.run_stream", side_effect=_one_token_turn):
            session = await agent.session("absent-safe")
            events = [event async for event in agent.run("say hello", session=session)]
        await agent.shutdown()
    return agent, events


def _module_roots_reaching_the_loader(agent: ArcAgent) -> set[str]:
    """The module names the loader actually took as a scan root.

    Read off the loader's own ``_scan_roots`` so a name here means a root the
    loader really holds, not a path the test recomputed.
    """
    assert agent._capability_loader is not None
    return {
        name.removeprefix("module:")
        for name, _ in agent._capability_loader._scan_roots
        if name.startswith("module:")
    }


def test_discovery_yields_modules_to_parametrize_over() -> None:
    """Without this, an empty discovery would make the whole suite vacuously green."""
    assert _MODULE_NAMES, "discover_modules() returned nothing — the suite below proves nothing"
    assert _SOURCE_CATALOG.is_dir()


def test_discovery_resolves_the_deployment_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-964's red: ``discover_modules()`` must read ``${ARC_CONFIG_DIR}/modules/``.

    Until it does, the deployment root is unreachable from the code that
    decides what loads, and the absent-safe cases below can only simulate what
    production does for real.
    """
    _deployment_root(tmp_path, absent=_MODULE_NAMES[0])
    monkeypatch.setenv("ARC_CONFIG_DIR", str(_arc_home(tmp_path)))

    assert discover_modules() == [n for n in _MODULE_NAMES if n != _MODULE_NAMES[0]]


@pytest.mark.parametrize("absent", _MODULE_NAMES)
async def test_a_turn_completes_with_one_module_absent(
    absent: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One module's tree is gone; construction, capability scan, and turn survive."""
    _deployment_root(tmp_path, absent=absent)
    _capability_copies(tmp_path, absent=absent)
    monkeypatch.setenv("ARC_CONFIG_DIR", str(_arc_home(tmp_path)))

    agent, events = await _run_a_turn(_agent_config(tmp_path), tmp_path)

    assert agent._capability_loader is not None
    loaded = _module_roots_reaching_the_loader(agent)
    assert absent not in loaded, "the removed module still reached the loader"
    # Exactly one fewer than the full set: proves the turn ran with the other
    # seventeen genuinely loaded, so a pass is not the trivial "nothing loaded".
    assert loaded == {n for n in _MODULE_NAMES if n != absent}
    assert isinstance(events[-1], TurnEndEvent)
    assert events[-1].final_text == "absent-safe"


async def test_a_turn_completes_with_every_module_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fresh-box case: a signed deployment where nothing has been installed yet."""
    module_root(_arc_home(tmp_path)).mkdir(parents=True)
    monkeypatch.setenv("ARC_CONFIG_DIR", str(_arc_home(tmp_path)))

    agent, events = await _run_a_turn(_agent_config(tmp_path), tmp_path)

    assert agent._capability_loader is not None
    assert not _module_roots_reaching_the_loader(agent)
    assert isinstance(events[-1], TurnEndEvent)
    assert events[-1].final_text == "absent-safe"
