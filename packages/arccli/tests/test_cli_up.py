"""``arc up`` brings the stack up, or refuses to pretend it did.

Every test runs against an isolated deployment: a temporary ``ARC_CONFIG_DIR``,
``ARCSTORE_DATA_DIR``, ``ARCTEAM_NATS_URL``, and a fake ``nats-server`` on PATH.
Nothing here may touch the developer's real ``~/.arc`` — a test in this project
once wrote into it, and a bring-up command is the worst possible place to repeat
that, since its whole job is installing modules and materializing state.

The module stage is exercised through the REAL install path — the same
verify → materialize → copy → enable sequence ``arc module install`` runs,
against the real module source catalog. Only two things are stubbed: the server
start (``ui_handler``), because uvicorn would block forever, and nothing else.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import arcagent
import pytest

from arccli.commands import up as up_cmd

#: The real repository catalog — the same tree release CI packages from.
_SOURCE_CATALOG = Path(__file__).resolve().parents[2] / "arcagent" / "src" / "arcagent" / "modules"

#: A module that exists in the catalog and is absent from a fresh deployment.
_MODULE = "memory"


def _free_port() -> int:
    """A port nothing is listening on, so the health probe fails fast and truly."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    return port


def _agent_toml(agent_dir: Path, *, tier: str = "personal", modules: tuple[str, ...] = ()) -> str:
    body = (
        "[agent]\n"
        "name = 'josh'\n"
        "org = 'testorg'\n"
        "type = 'executor'\n"
        f"workspace = '{agent_dir / 'workspace'}'\n\n"
        "[llm]\n"
        "model = 'test/model'\n\n"
        "[identity]\n"
        f"key_dir = '{agent_dir / 'keys'}'\n\n"
        "[telemetry]\n"
        "enabled = false\n\n"
        f"[security]\ntier = '{tier}'\n"
    )
    for name in modules:
        body += f"\n[modules.{name}]\nenabled = true\n"
    return body


@pytest.fixture
def deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """An isolated deployment: tmp arc home, tmp store, fake nats-server, one agent."""
    root = tmp_path / "deployment"
    (root / "arc").mkdir(parents=True)
    (root / "store").mkdir()
    monkeypatch.setenv("ARC_CONFIG_DIR", str(root / "arc"))
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(root / "store"))
    monkeypatch.setenv("ARC_MODULE_SOURCE", str(_SOURCE_CATALOG))
    # A port nothing is bound to, so "NATS reachable" is a real observation.
    monkeypatch.setenv("ARCTEAM_NATS_URL", f"nats://127.0.0.1:{_free_port()}")
    monkeypatch.chdir(root)

    # A fake nats-server on PATH, so the preflight's shutil.which is the real one.
    fake_bin = root / "bin"
    fake_bin.mkdir()
    nats = fake_bin / "nats-server"
    nats.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    nats.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{__import__('os').environ['PATH']}")

    from arccli.commands.operator import ensure_operator_key

    ensure_operator_key(root / "arc")

    agent = root / "team" / "josh_agent"
    (agent / "workspace").mkdir(parents=True)
    (agent / "arcagent.toml").write_text(_agent_toml(agent), encoding="utf-8")
    yield root


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record every ``arc ui start`` invocation instead of blocking on uvicorn."""
    calls: list[list[str]] = []

    def _record(argv: list[str]) -> None:
        calls.append(argv)

    monkeypatch.setattr("arccli.commands.ui.ui_handler", _record)
    return calls


@pytest.fixture(autouse=True)
def _no_health_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the post-start health watcher out of the tests' way.

    It is a daemon thread whose only effect is a printed line; leaving it
    running would have every test pay its poll timeout on a dead port.
    """
    monkeypatch.setattr(up_cmd, "watch_health", lambda host, port: None)


def _enable(deployment: Path, module: str, *, tier: str = "personal") -> Path:
    """Enable ``module`` in the agent's config without installing it."""
    agent = deployment / "team" / "josh_agent"
    (agent / "arcagent.toml").write_text(
        _agent_toml(agent, tier=tier, modules=(module,)), encoding="utf-8"
    )
    return agent


def _run(*args: str, port: int | None = None) -> None:
    up_cmd.up_handler([*args, "--port", str(port or _free_port()), "--no-browser"])


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def test_preflight_fails_loudly_when_nats_server_is_absent(
    deployment: Path, started: list[list[str]], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """No broker binary means dead messaging and dead task dispatch — so: stop.

    Both lookup sources are emptied. The resolver searches the usual install
    directories after ``$PATH`` — that widening is what stopped a correctly
    provisioned box being refused a deploy — so clearing ``PATH`` alone would
    leave a developer's own ``/opt/homebrew/bin/nats-server`` answering and this
    test asserting nothing at all.
    """
    monkeypatch.setenv("PATH", str(deployment / "empty-bin"))
    monkeypatch.setattr("arcteam.nats_server._WELL_KNOWN_BIN_DIRS", ())

    with pytest.raises(SystemExit) as exit_info:
        _run()

    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    assert "nats-server" in captured.out
    assert "FAIL" in captured.out
    assert "preflight failed" in captured.err
    assert started == [], "a failed preflight must not start anything"


def test_preflight_fails_when_no_operator_key_exists(
    deployment: Path, started: list[list[str]], capsys: Any
) -> None:
    """An unpinned operator is no operator — and this command never mints one."""
    from arccli.commands.operator import operator_key_path

    operator_key_path(deployment / "arc").unlink()

    with pytest.raises(SystemExit) as exit_info:
        _run()

    assert exit_info.value.code == 1
    assert "operator key" in capsys.readouterr().out
    assert started == []
    assert not operator_key_path(deployment / "arc").exists(), "preflight minted a key"


def test_preflight_fails_when_the_team_root_holds_no_agent(
    deployment: Path, started: list[list[str]], capsys: Any
) -> None:
    """Nothing to bring up is a failure, not a quiet success.

    Every other precondition is satisfied by the fixture, so the team-root
    check is provably the one that failed rather than merely one FAIL among
    several.
    """
    empty = deployment / "no-agents"
    empty.mkdir()
    checks = {check.name: check for check in up_cmd.preflight(empty)}
    assert checks["team root"].ok is False
    assert all(check.ok for name, check in checks.items() if name != "team root")

    with pytest.raises(SystemExit) as exit_info:
        _run("--team-root", str(empty))

    assert exit_info.value.code == 1
    assert "arc agent create" in capsys.readouterr().out
    assert started == []


# ---------------------------------------------------------------------------
# Module bootstrap
# ---------------------------------------------------------------------------


def test_a_config_enabled_but_absent_module_is_installed_and_then_starts(
    deployment: Path, started: list[list[str]], capsys: Any
) -> None:
    """The bootstrap that closes the gap: enabled in config, absent on disk, installed."""
    agent = _enable(deployment, _MODULE)
    assert _MODULE not in arcagent.discover_modules(), "the module was already installed"

    _run()

    # Materialized at the deployment root — the folder the agent actually scans.
    assert (deployment / "arc" / "modules" / _MODULE / "_runtime.py").is_file()
    assert _MODULE in arcagent.discover_modules()
    # And the per-agent capability copy the install path also writes.
    from arcbundle import capability_dir

    assert (capability_dir(agent, _MODULE) / "capabilities.py").is_file()

    captured = capsys.readouterr()
    assert "installed" in captured.out
    assert started, "a whole deployment must actually start"
    assert "--team-root" in started[0]


def test_a_present_module_is_reported_and_not_reinstalled(
    deployment: Path, started: list[list[str]], capsys: Any
) -> None:
    """Second run of the same bring-up is a report, not a second install."""
    _enable(deployment, _MODULE)
    _run()
    marker = deployment / "arc" / "modules" / _MODULE / "_runtime.py"
    first = marker.stat().st_mtime_ns

    capsys.readouterr()
    _run()

    assert marker.stat().st_mtime_ns == first, "an already-present module was rewritten"
    assert "already materialized" in capsys.readouterr().out
    assert len(started) == 2


def test_federal_refuses_a_dev_signed_auto_install(
    deployment: Path, started: list[list[str]], capsys: Any
) -> None:
    """A hardened box does not get a development signature as a convenience.

    The refusal has to name the alternative — bundle on the low side, stage
    here — or an operator's only option is to widen the trust rules.
    """
    _enable(deployment, _MODULE, tier="federal")

    with pytest.raises(SystemExit) as exit_info:
        _run()

    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    assert "REFUSED" in captured.out
    assert "arc module bundle" in captured.out
    assert not (deployment / "arc" / "modules" / _MODULE).exists(), "federal got the module anyway"
    assert started == [], "a federal box must not start with a capability it was denied"


def test_no_install_skips_the_module_stage_entirely(
    deployment: Path, started: list[list[str]], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """--no-install inspects; it never writes. The install path must not be reached."""

    def _never(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("--no-install reached the install path")

    monkeypatch.setattr("arccli.commands.module.install_module_for_agent", _never)
    _enable(deployment, _MODULE)

    with pytest.raises(SystemExit) as exit_info:
        _run("--no-install")

    assert exit_info.value.code == 1  # still missing, so still degraded
    assert "--no-install" in capsys.readouterr().out
    assert not (deployment / "arc" / "modules" / _MODULE).exists()


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


def test_a_stack_that_would_be_degraded_is_never_started(
    deployment: Path, started: list[list[str]], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """The point of the whole command.

    An install that fails for any reason leaves a module enabled in config and
    absent on disk. The agent would boot, answer chat, and never fire a
    schedule. That must exit non-zero with the module named, and must not reach
    the server start.
    """

    def _fails(module: str, **kwargs: Any) -> str:
        from arccli.commands.module import ModuleInstallError

        raise ModuleInstallError("bundle store unreachable")

    monkeypatch.setattr("arccli.commands.module.install_module_for_agent", _fails)
    _enable(deployment, _MODULE)

    with pytest.raises(SystemExit) as exit_info:
        _run()

    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    assert "MISSING" in captured.out
    assert f"DEGRADED: josh enables module '{_MODULE}'" in captured.err
    assert f"arc module install {_MODULE} --agent josh" in captured.err
    assert started == [], "a degraded stack was started — that is the whole failure mode"


def test_check_starts_nothing_and_exits_non_zero_on_a_missing_module(
    deployment: Path, started: list[list[str]], capsys: Any
) -> None:
    """--check is the pre-deploy gate: it reports, it never installs, it never starts."""
    _enable(deployment, _MODULE)

    with pytest.raises(SystemExit) as exit_info:
        _run("--check")

    assert exit_info.value.code == 1
    assert "MISSING" in capsys.readouterr().out
    assert not (deployment / "arc" / "modules" / _MODULE).exists(), "--check installed something"
    assert started == []


def test_check_passes_and_starts_nothing_on_a_whole_deployment(
    deployment: Path, started: list[list[str]], capsys: Any
) -> None:
    """A clean --check exits zero and still starts nothing."""
    up_cmd.up_handler(["--check", "--port", str(_free_port())])

    captured = capsys.readouterr()
    assert "the deployment is whole" in captured.out
    assert "DEGRADED" not in captured.err
    assert started == []


def test_verify_reports_nats_and_health_as_observed_state(deployment: Path) -> None:
    """Service rows are observations, never verdicts — before a start they are down."""
    assert up_cmd.probe_nats().up is False
    assert up_cmd.probe_health("127.0.0.1", _free_port()).up is False


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_up_is_registered_in_the_command_registry() -> None:
    """One dispatcher. ``arc up`` resolves through it like every other verb."""
    from arccli.commands.registry import resolve_command

    command = resolve_command("up")
    assert command is not None
    assert command.cli_only is True
    assert command.handler is not None


def test_start_argv_passes_the_operator_flags_through(deployment: Path) -> None:
    """Whatever the operator asked for reaches ``arc ui start`` unmodified."""
    parsed = up_cmd._build_parser().parse_args(
        ["--port", "9999", "--host", "10.1.2.3", "--no-browser", "--gateway-config", "g.toml"]
    )
    argv = up_cmd.ui_start_argv(parsed, deployment / "team")

    assert argv[0] == "start"
    assert argv[argv.index("--port") + 1] == "9999"
    assert argv[argv.index("--host") + 1] == "10.1.2.3"
    assert argv[argv.index("--gateway-config") + 1] == "g.toml"
    assert argv[argv.index("--team-root") + 1] == str(deployment / "team")
    assert "--no-browser" in argv
