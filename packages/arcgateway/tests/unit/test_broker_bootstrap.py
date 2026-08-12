"""COMP-008 BrokerBootstrap — broker guaranteed on every launch path (T-942, T-943).

REQ-306: embedded gateway startup SHALL ensure a message broker before serving
messaging, on every launch path, with no operator step beyond starting Arc.
REQ-307: when no broker can be started or reached, the messaging surfaces SHALL
report an explicit unavailable state — never an empty inbox presented as an
accurate one — and the failure SHALL be logged loudly.

The defect these cover: ``arcteam.nats_server.ensure_nats_server`` exists and
works, but has exactly ONE caller (``arccli.commands._serve.bootstrap_infra``,
the ``arc … serve`` path). ``arcgateway.bootstrap.build_for_embedded`` — the
composition root every other launch path goes through, including the one arcui
hosts — never calls it. So on a plain start the broker is simply not there and
the inbox reads empty.

No live broker, no network, no sleeps. ``ensure_nats_server`` is replaced with a
recorder at its CANONICAL module path (``arcteam.nats_server``) *and* on the
consumer module when it binds the name at import time, so a fresh venv cannot
fall through to the real implementation. As belt and braces the real probe and
the ``nats-server`` lookup are poisoned: if anything reaches them the test fails
loudly instead of spawning a broker.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from arcteam.nats_server import NatsServerUnavailableError

from arcgateway.config import GatewayConfig

_BROKER_MODULE = "arcgateway.broker_bootstrap"


# ── Fakes ─────────────────────────────────────────────────────────────────────


@dataclass
class _FakeChild:
    """Stand-in for ``ManagedNatsServer`` — records its own teardown."""

    terminate_calls: int = 0

    def terminate_sync(self) -> None:
        self.terminate_calls += 1

    @property
    def terminated(self) -> bool:
        return self.terminate_calls > 0


@dataclass
class _EnsureRecorder:
    """Replacement for ``ensure_nats_server``: records calls, spawns nothing.

    ``spawn`` mirrors the real contract — return a handle when this call started
    a child, ``None`` when an already-running broker was reused, or raise
    ``NatsServerUnavailableError`` when neither is possible.
    """

    spawn: bool = True
    raises: NatsServerUnavailableError | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)
    children: list[_FakeChild] = field(default_factory=list)

    async def __call__(self, **kwargs: Any) -> _FakeChild | None:
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        if not self.spawn:
            return None
        child = _FakeChild()
        self.children.append(child)
        return child

    @property
    def call_count(self) -> int:
        return len(self.calls)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_real_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make it impossible for this module to touch a real broker.

    ``broker_listening`` is the real code's TCP probe and ``shutil.which`` is how
    it finds ``nats-server`` to spawn. Poisoning both means a missed patch surfaces
    as an explicit failure rather than as a test that quietly reuses (or starts) a
    broker on the developer's machine.
    """
    import arcteam.nats_server as nats_server

    async def _probe_forbidden(*_args: Any, **_kwargs: Any) -> bool:
        raise AssertionError("test reached the real broker TCP probe")

    def _which_forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("test reached the real nats-server lookup")

    monkeypatch.setattr(nats_server, "broker_listening", _probe_forbidden)
    monkeypatch.setattr("shutil.which", _which_forbidden)


def _install_ensure(monkeypatch: pytest.MonkeyPatch, recorder: _EnsureRecorder) -> None:
    """Patch ``ensure_nats_server`` at the canonical path and on the consumer.

    Patching only an already-imported name is the recorded failure mode that lets
    real calls through in a fresh venv, so the canonical module is always patched
    and the consumer module is patched too whenever it binds the name itself.
    """
    import arcteam.nats_server as nats_server

    monkeypatch.setattr(nats_server, "ensure_nats_server", recorder)
    try:
        consumer = importlib.import_module(_BROKER_MODULE)
    except ImportError:
        return
    if hasattr(consumer, "ensure_nats_server"):
        monkeypatch.setattr(consumer, "ensure_nats_server", recorder)


def _config(toml: str = "") -> GatewayConfig:
    return GatewayConfig.from_toml_str(toml)


@pytest.fixture
def team_root(tmp_path: Path) -> Path:
    root = tmp_path / "team"
    root.mkdir()
    return root


# ── T-942: the broker is ensured on every launch path ─────────────────────────


class TestBrokerEnsuredOnEveryLaunchPath:
    """REQ-306 — starting Arc is the only step; the broker follows from it."""

    @pytest.mark.asyncio
    async def test_start_broker_ensures_a_broker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """COMP-008 delegates to the existing, working ``ensure_nats_server``."""
        recorder = _EnsureRecorder(spawn=True)
        _install_ensure(monkeypatch, recorder)
        broker_bootstrap = importlib.import_module(_BROKER_MODULE)

        handle = await broker_bootstrap.start_broker()

        assert recorder.call_count == 1
        assert handle.available is True
        assert handle.reason is None

    @pytest.mark.asyncio
    async def test_start_broker_passes_url_and_store_dir(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SDD inputs: a configured broker url and a JetStream store dir."""
        recorder = _EnsureRecorder(spawn=True)
        _install_ensure(monkeypatch, recorder)
        broker_bootstrap = importlib.import_module(_BROKER_MODULE)

        await broker_bootstrap.start_broker()

        kwargs = recorder.calls[0]
        assert isinstance(kwargs["url"], str)
        assert kwargs["url"]
        assert isinstance(kwargs["store_dir"], Path)

    @pytest.mark.asyncio
    async def test_embedded_startup_ensures_a_broker(
        self, monkeypatch: pytest.MonkeyPatch, team_root: Path
    ) -> None:
        """THE defect: build_for_embedded does not ensure a broker at all today.

        Every launch path except ``arc … serve`` composes through this function,
        so a broker that is only started by the CLI is a broker most deployments
        never get.
        """
        recorder = _EnsureRecorder(spawn=True)
        _install_ensure(monkeypatch, recorder)
        from arcgateway.bootstrap import build_for_embedded

        bundle = await build_for_embedded(team_root, _config())

        assert recorder.call_count == 1, "embedded startup never ensured a broker"
        assert bundle.broker.available is True

    @pytest.mark.asyncio
    async def test_plain_start_needs_no_operator_step(
        self, monkeypatch: pytest.MonkeyPatch, team_root: Path
    ) -> None:
        """An empty config — no flag, no extra command — still gets a broker."""
        recorder = _EnsureRecorder(spawn=True)
        _install_ensure(monkeypatch, recorder)
        from arcgateway.bootstrap import build_for_embedded

        bundle = await build_for_embedded(team_root, _config(""))

        assert recorder.call_count == 1
        assert bundle.broker.available is True

    @pytest.mark.asyncio
    async def test_running_broker_is_reused_not_duplicated(
        self, monkeypatch: pytest.MonkeyPatch, team_root: Path
    ) -> None:
        """A broker already listening is reused; no second child is spawned.

        ``ensure_nats_server`` signals reuse by returning ``None`` — the caller
        owns nothing to stop. Starting twice must not leave two brokers behind.
        """
        recorder = _EnsureRecorder(spawn=False)
        _install_ensure(monkeypatch, recorder)
        from arcgateway.bootstrap import build_for_embedded

        first = await build_for_embedded(team_root, _config())
        second = await build_for_embedded(team_root, _config())

        assert recorder.children == [], "a second broker child was spawned"
        assert first.broker.available is True
        assert second.broker.available is True
        assert first.broker.managed is None
        assert second.broker.managed is None


# ── T-942: lifecycle — a child never outlives its parent ──────────────────────


class TestChildTornDownWithParent:
    """The SDD's named risk: a supervised child that outlives the gateway."""

    @pytest.mark.asyncio
    async def test_aclose_terminates_the_child_it_started(
        self, monkeypatch: pytest.MonkeyPatch, team_root: Path
    ) -> None:
        recorder = _EnsureRecorder(spawn=True)
        _install_ensure(monkeypatch, recorder)
        from arcgateway.bootstrap import build_for_embedded

        bundle = await build_for_embedded(team_root, _config())
        child = recorder.children[0]
        assert child.terminated is False

        await bundle.broker.aclose()

        assert child.terminated is True

    @pytest.mark.asyncio
    async def test_aclose_is_idempotent(
        self, monkeypatch: pytest.MonkeyPatch, team_root: Path
    ) -> None:
        """Shutdown runs from more than one place; a second close must be safe."""
        recorder = _EnsureRecorder(spawn=True)
        _install_ensure(monkeypatch, recorder)
        from arcgateway.bootstrap import build_for_embedded

        bundle = await build_for_embedded(team_root, _config())
        await bundle.broker.aclose()
        await bundle.broker.aclose()

        assert recorder.children[0].terminate_calls <= 1

    @pytest.mark.asyncio
    async def test_reused_broker_is_never_terminated(
        self, monkeypatch: pytest.MonkeyPatch, team_root: Path
    ) -> None:
        """Closing our handle must not kill a broker someone else owns."""
        recorder = _EnsureRecorder(spawn=False)
        _install_ensure(monkeypatch, recorder)
        from arcgateway.bootstrap import build_for_embedded

        bundle = await build_for_embedded(team_root, _config())
        await bundle.broker.aclose()

        assert recorder.children == []

    @pytest.mark.asyncio
    async def test_child_is_torn_down_when_a_later_startup_stage_fails(
        self, monkeypatch: pytest.MonkeyPatch, team_root: Path
    ) -> None:
        """A leak on the error path is still a leak.

        The broker must be ensured as part of startup, which means a stage that
        fails after it must not strand the child. ``start_runner_host`` is the
        last thing ``build_for_embedded`` does, so failing it exercises the
        error path regardless of where the broker start is placed.
        """
        recorder = _EnsureRecorder(spawn=True)
        _install_ensure(monkeypatch, recorder)

        async def _explode(**_kwargs: Any) -> Any:
            raise RuntimeError("late startup stage failed")

        monkeypatch.setattr("arcgateway.workflow_runner_host.start_runner_host", _explode)
        from arcgateway.bootstrap import build_for_embedded

        with pytest.raises(RuntimeError):
            await build_for_embedded(team_root, _config())

        assert recorder.children, "no broker child was started before the failure"
        assert recorder.children[0].terminated is True, "startup failed and leaked the broker"


# ── T-943: no broker means unavailable, never an empty inbox ──────────────────


class TestBrokerAbsentReportsUnavailable:
    """REQ-307 — 'I cannot see your messages' must not read as 'no messages'."""

    @pytest.mark.asyncio
    async def test_start_broker_returns_explicit_unavailable_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No broker and none startable: an explicit state, not a bare ``None``."""
        recorder = _EnsureRecorder(raises=NatsServerUnavailableError("nats-server not on PATH"))
        _install_ensure(monkeypatch, recorder)
        broker_bootstrap = importlib.import_module(_BROKER_MODULE)

        handle = await broker_bootstrap.start_broker()

        assert handle is not None
        assert handle.available is False
        assert handle.reason
        assert "nats-server not on PATH" in handle.reason

    @pytest.mark.asyncio
    async def test_unavailable_broker_is_logged_loudly(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """'The log says so loudly' — an ERROR record naming the reason.

        A warning buried in a connect helper is what the deployment has today,
        and it is why an operator reads an empty inbox and believes it.
        """
        recorder = _EnsureRecorder(raises=NatsServerUnavailableError("nats-server not on PATH"))
        _install_ensure(monkeypatch, recorder)
        broker_bootstrap = importlib.import_module(_BROKER_MODULE)

        with caplog.at_level("ERROR"):
            await broker_bootstrap.start_broker()

        loud = [r for r in caplog.records if r.levelno >= 40]
        assert loud, "broker unavailable was not logged at ERROR"
        assert any("nats-server not on PATH" in r.getMessage() for r in loud)

    @pytest.mark.asyncio
    async def test_embedded_startup_survives_and_reports_unavailable(
        self, monkeypatch: pytest.MonkeyPatch, team_root: Path
    ) -> None:
        """The gateway still boots — but it says the broker is missing.

        Fail-open on serving, fail-loud on truth: the dashboard and web chat
        keep working, and the messaging surface carries an explicit unavailable
        state instead of an inbox that looks merely empty.
        """
        recorder = _EnsureRecorder(raises=NatsServerUnavailableError("broker unreachable"))
        _install_ensure(monkeypatch, recorder)
        from arcgateway.bootstrap import build_for_embedded

        bundle = await build_for_embedded(team_root, _config())

        assert bundle.session_router is not None
        assert bundle.broker.available is False
        assert bundle.broker.reason
        assert "broker unreachable" in bundle.broker.reason

    @pytest.mark.asyncio
    async def test_unavailable_is_distinguishable_from_a_healthy_empty_broker(
        self, monkeypatch: pytest.MonkeyPatch, team_root: Path
    ) -> None:
        """The whole point of REQ-307, stated as one comparison.

        A reachable broker with nothing in it and an unreachable broker must not
        produce the same answer for a caller deciding what to show an operator.
        """
        healthy = _EnsureRecorder(spawn=True)
        _install_ensure(monkeypatch, healthy)
        from arcgateway.bootstrap import build_for_embedded

        up = await build_for_embedded(team_root, _config())

        down_recorder = _EnsureRecorder(raises=NatsServerUnavailableError("broker unreachable"))
        _install_ensure(monkeypatch, down_recorder)
        down = await build_for_embedded(team_root, _config())

        assert up.broker.available is True
        assert down.broker.available is False
        assert up.broker.available != down.broker.available
