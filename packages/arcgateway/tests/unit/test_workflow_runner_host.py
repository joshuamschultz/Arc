"""RunnerHost — SPEC-061 COMP-009: lifecycle + singleton enforcement (REQ-231).

``arcteam.workflows.runner`` (COMP-008) is being built concurrently on a
sibling branch and does not exist in this checkout — several tests below
exercise that REAL absence (the default factory's clean, fail-open
degradation) rather than mocking it away, alongside tests that inject a fake
runner implementing :class:`WorkflowRunnerProtocol` to exercise the
lifecycle/singleton machinery this package owns.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from arcgateway.workflow_runner_host import (
    RunnerAlreadyActiveError,
    RunnerHost,
    start_runner_host,
)


@pytest.fixture(autouse=True)
def _reset_singleton() -> Any:
    """Every test starts with a clean process-wide slot and leaves one behind."""
    RunnerHost._active = None
    yield
    RunnerHost._active = None


class _FakeRunner:
    """A minimal stand-in for arcteam's WorkflowRunner (COMP-008)."""

    def __init__(self) -> None:
        self.ticks = 0
        self.closed = False
        self._stop = asyncio.Event()

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            self.ticks += 1
            await asyncio.sleep(0.01)

    async def aclose(self) -> None:
        self.closed = True
        self._stop.set()


# ---------------------------------------------------------------------------
# RunnerHost lifecycle
# ---------------------------------------------------------------------------


async def test_start_runs_the_runner_and_registers_the_singleton() -> None:
    runner = _FakeRunner()

    host = await RunnerHost.start(runner)

    assert RunnerHost.active() is host
    await asyncio.sleep(0.03)
    assert runner.ticks > 0

    await host.stop()
    assert RunnerHost.active() is None
    assert runner.closed is True


async def test_second_start_refuses_rather_than_racing_the_frontier() -> None:
    """REQ-231: two runners must never advance the same frontier."""
    first = await RunnerHost.start(_FakeRunner())

    with pytest.raises(RunnerAlreadyActiveError):
        await RunnerHost.start(_FakeRunner())

    # The first host is still the one and only active runner.
    assert RunnerHost.active() is first
    await first.stop()


async def test_stop_survives_a_runner_that_raises_on_close() -> None:
    """Shutdown must clear the singleton slot even if aclose() itself errors."""

    class _BrokenRunner(_FakeRunner):
        async def aclose(self) -> None:
            raise RuntimeError("boom")

    host = await RunnerHost.start(_BrokenRunner())
    await host.stop()

    assert RunnerHost.active() is None


# ---------------------------------------------------------------------------
# start_runner_host — the bootstrap-facing entry point
# ---------------------------------------------------------------------------


async def test_start_runner_host_starts_a_real_runner() -> None:
    """The default path builds arcteam's real engine and starts it.

    This test previously asserted ``host is None`` — the fail-open behaviour of
    a checkout whose arcteam half had not landed. That half has landed, so
    asserting None would now be asserting the dead wiring: everything boots,
    nothing errors, and no runner ever advances a frontier. The fail-open
    contract is still covered, by the test below that makes construction raise.
    """
    host = await start_runner_host(tier="personal")

    assert host is not None, "the gateway booted with no runner — dead wiring"
    assert RunnerHost.active() is host
    await host.stop()


async def test_start_runner_host_still_fails_open_when_construction_raises() -> None:
    """A genuine construction error degrades the gateway; it never aborts boot."""

    async def _explodes(*, tier: str, key_path: Path) -> Any:
        raise RuntimeError("arcteam engine unavailable")

    host = await start_runner_host(tier="personal", runner_factory=_explodes)

    assert host is None
    assert RunnerHost.active() is None


async def test_start_runner_host_uses_an_injected_factory() -> None:
    fake = _FakeRunner()
    seen: dict[str, Any] = {}

    async def _factory(*, tier: str, key_path: Path) -> _FakeRunner:
        seen["tier"] = tier
        seen["key_path"] = key_path
        return fake

    host = await start_runner_host(tier="federal", runner_factory=_factory)

    assert host is not None
    assert RunnerHost.active() is host
    assert seen["tier"] == "federal"
    assert isinstance(seen["key_path"], Path)
    await host.stop()


async def test_start_runner_host_is_idempotent_within_a_process() -> None:
    """A second call in the same process returns the existing host, never a race."""
    fake = _FakeRunner()

    async def _factory(*, tier: str, key_path: Path) -> _FakeRunner:
        return fake

    first = await start_runner_host(tier="personal", runner_factory=_factory)
    second = await start_runner_host(tier="personal", runner_factory=_factory)

    assert first is second
    assert first is not None
    await first.stop()


# ---------------------------------------------------------------------------
# Removability (REQ-230/REQ-257 contribution from this package)
# ---------------------------------------------------------------------------


def test_runner_host_module_has_no_arcui_dependency() -> None:
    """The runner-hosting code must not import arcui.

    AST-scans actual import statements (not a docstring substring match, and
    not ``sys.modules`` — arcui may already be imported by an unrelated test
    in the same pytest process) proving the runner works whether or not
    arcui is installed — the whole point of hosting it in
    arcgateway.bootstrap rather than arcui's lifespan.
    """
    import ast

    import arcgateway.bootstrap as bootstrap_mod
    import arcgateway.workflow_runner_host as host_mod

    for mod in (bootstrap_mod, host_mod):
        assert mod.__file__ is not None
        tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            assert not any(n.split(".")[0] == "arcui" for n in names), (
                f"{mod.__name__} imports arcui at line {node.lineno}"
            )


# ---------------------------------------------------------------------------
# Anti-fail-open (SPEC-061 REQ-230/231)
#
# This feature's stated top risk is "producers unwired": correct predicates
# shipped with dead activating wiring. Fail-open is its worst form — the
# gateway boots, nothing errors, and every gate the feature builds is bypassed
# by simply never running. These tests exist so that state cannot be silent.
# ---------------------------------------------------------------------------


def test_the_host_names_the_module_that_actually_exists() -> None:
    """The host resolves arcteam's runner by name, so a typo is invisible.

    It shipped importing `arcteam.workflows.runner` (plural) — a module that
    exists in no checkout — and failed open, so no runner ever started and
    nothing said so. Importing the real module here is what makes that class
    of typo a red test instead of a silent no-op.
    """
    import importlib
    import inspect

    from arcgateway import workflow_runner_host

    source = inspect.getsource(workflow_runner_host)
    assert "arcteam.workflows" not in source, (
        "the host names arcteam.workflows (plural); the package is arcteam.workflow"
    )
    importlib.import_module("arcteam.workflow.runner")


async def test_a_runner_that_cannot_be_built_is_reported_not_swallowed() -> None:
    """Fail-open is allowed; failing SILENTLY is not.

    A checkout without the engine may still boot the gateway — but the operator
    has to be able to tell that no workflow will ever progress. The host must
    surface the reason rather than returning None with an empty log.
    """
    import logging

    from arcgateway.workflow_runner_host import start_runner_host

    def _explodes(**_: object) -> object:
        raise RuntimeError("engine absent in this checkout")

    logger = logging.getLogger("arcgateway.workflow_runner_host")
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    logger.addHandler(handler)
    try:
        host = await start_runner_host(tier="personal", runner_factory=_explodes)
    finally:
        logger.removeHandler(handler)

    assert host is None, "an unbuildable runner must not yield a live host"
    assert records, "a gateway with no workflow runner must say so in the log"
    assert any("engine absent" in r.getMessage() or r.exc_info for r in records), (
        "the log line must carry the reason, not just note an absence"
    )


async def test_a_started_runner_reaches_the_agent_tool_surface() -> None:
    """The gateway hosting a runner is not enough — the tool must be able to use it.

    Both halves can be individually healthy while the feature does nothing: a
    live runner in the gateway, and `workflow_run` reporting that no runner is
    hosted here. `set_runner` existed and was never called, which is the same
    silent shape as the plural-module-name bug this suite already guards.
    """
    from pathlib import Path

    from arcagent.modules.workflows import _runtime
    from arctrust import AgentIdentity

    from arcgateway.workflow_runner_host import start_runner_host

    class _Runner:
        async def run_forever(self) -> None:
            import asyncio

            await asyncio.sleep(3600)

        async def aclose(self) -> None:
            return None

    runner = _Runner()

    async def _factory(**_: object) -> _Runner:
        return runner

    _runtime.reset()
    host = await start_runner_host(tier="personal", runner_factory=_factory)
    try:
        assert host is not None
        # Publish-then-configure: the gateway starts the runner before this
        # agent binds its module. The runner must survive that ordering.
        _runtime.configure(
            identity=AgentIdentity.generate(org="local", agent_type="agent"),
            workspace=Path("."),
        )
        assert _runtime.state().runner is runner, (
            "the gateway started a runner but never published it to the agent tools; "
            "workflow_run would refuse every run on a deployment that looks healthy"
        )
    finally:
        if host is not None:
            await host.stop()
        _runtime.reset()
