"""``arc up`` on a fleet where one agent's config cannot be read (SPEC-066).

A single agent is the easy case. The shape that actually reaches a deployment is
a *fleet* in which one member is broken and the rest are fine — and the tempting
behaviour there is the wrong one: install what you can, start the stack, and let
the broken agent be somebody's later problem. That agent boots, answers chat,
and never fires a schedule, which is precisely the silent degradation ``arc up``
exists to make loud.

So the contract exercised here is three-part and must hold **together**:

* the healthy agent is installed and reported normally — one bad config must not
  cost the fleet its capabilities;
* the broken one is reported ``UNREADABLE`` with the reason attached, in both
  the module table and the verify table;
* the **exit code is non-zero and nothing is started**.

The broken config is valid TOML that fails schema validation (``enabled =
"yes-please"``), which is both the realistic operator typo and the only shape
that reaches this code: ``arcgateway.team_roster`` drops an agent whose TOML
does not *parse* at all, so a syntax error would never make it as far as
``agent_states``.

Isolation is the same two env vars every other suite here uses. Nothing may
touch the developer's real ``~/.arc`` — a bring-up command installs modules and
materializes state, so a leak would write real bytes into a real deployment.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import arcagent
import pytest
from arctrust.paths import module_root

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


def _healthy_toml(agent_dir: Path, *, name: str, modules: tuple[str, ...] = ()) -> str:
    body = (
        "[agent]\n"
        f"name = '{name}'\n"
        "org = 'testorg'\n"
        "type = 'executor'\n"
        f"workspace = '{agent_dir / 'workspace'}'\n\n"
        "[llm]\n"
        "model = 'test/model'\n\n"
        "[identity]\n"
        f"key_dir = '{agent_dir / 'keys'}'\n\n"
        "[telemetry]\n"
        "enabled = false\n\n"
        "[security]\ntier = 'personal'\n"
    )
    for module in modules:
        body += f"\n[modules.{module}]\nenabled = true\n"
    return body


def _unreadable_toml(name: str) -> str:
    """Valid TOML whose schema is wrong — the state that reaches ``load_config``.

    A TOML *syntax* error is dropped by the roster before ``arc up`` ever sees
    it, so testing the UNREADABLE path with one would prove nothing. A boolean
    field holding a string parses, reaches ``arcagent.load_config``, and fails
    validation there — which is the real operator mistake as well.
    """
    return f"[agent]\nname = '{name}'\n\n[modules.{_MODULE}]\nenabled = 'yes-please'\n"


@pytest.fixture
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Two agents under one team root: ``healthy`` works, ``broken`` does not."""
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

    healthy = root / "team" / "healthy_agent"
    (healthy / "workspace").mkdir(parents=True)
    (healthy / "arcagent.toml").write_text(
        _healthy_toml(healthy, name="healthy", modules=(_MODULE,)), encoding="utf-8"
    )

    broken = root / "team" / "broken_agent"
    broken.mkdir(parents=True)
    (broken / "arcagent.toml").write_text(_unreadable_toml("broken"), encoding="utf-8")

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
    """Keep the post-start health watcher out of the tests' way."""
    monkeypatch.setattr(up_cmd, "watch_health", lambda host, port: None)


def _run(*args: str) -> None:
    up_cmd.up_handler([*args, "--port", str(_free_port()), "--no-browser"])


def _rows_for(out: str, agent_id: str) -> list[str]:
    """Every table line describing ``agent_id``, so a match cannot be borrowed.

    An agent contributes several rows — a summary line plus one per module — so
    matching on the leading cell rather than a substring is what keeps an
    assertion about one agent from passing on another agent's row.
    """
    return [line for line in out.splitlines() if line.split()[:1] == [agent_id]]


# ---------------------------------------------------------------------------
# The whole contract, end to end
# ---------------------------------------------------------------------------


def test_a_fleet_with_one_unreadable_config_installs_the_rest_and_still_refuses_to_start(
    fleet: Path, started: list[list[str]], capsys: Any
) -> None:
    """The single most important shape ``arc up`` handles.

    All three halves are asserted in one test on purpose: they are only correct
    together. Installing the healthy agent while exiting zero would start a
    fleet with a dead member; exiting non-zero while skipping the healthy
    agent's install would punish the fleet for one bad file.
    """
    assert _MODULE not in arcagent.discover_modules(), "the module was already installed"

    with pytest.raises(SystemExit) as exit_info:
        _run()

    # 1. Non-zero, and nothing started. The whole reason the command exists.
    assert exit_info.value.code == 1
    assert started == [], "a fleet with an unreadable agent was started"

    captured = capsys.readouterr()

    # 2. The healthy agent really was installed — materialized at the deployment
    #    root the agent scans, copied into its own tree, and reported as such.
    assert (module_root(fleet / "arc") / _MODULE / "_runtime.py").is_file()
    assert _MODULE in arcagent.discover_modules()
    from arcbundle import capability_dir

    healthy = fleet / "team" / "healthy_agent"
    assert (capability_dir(healthy, _MODULE) / "capabilities.py").is_file()
    assert any("installed" in row for row in _rows_for(captured.out, "healthy"))

    # 3. The broken agent is named, with the reason, in the table and on stderr.
    assert all("UNREADABLE" in row for row in _rows_for(captured.out, "broken"))
    assert "CONFIG_VALIDATION" in captured.out, "the UNREADABLE row carried no detail"
    assert "DEGRADED: broken — config unreadable" in captured.err


def test_the_healthy_agent_is_not_reported_as_degraded_by_its_broken_sibling(
    fleet: Path, started: list[list[str]], capsys: Any
) -> None:
    """The non-zero exit must be attributable to exactly one agent.

    Without this, the test above would still pass if ``arc up`` failed the whole
    fleet on any error — and an operator reading DEGRADED lines for agents that
    are fine has no way to find the one that is not.
    """
    with pytest.raises(SystemExit):
        _run()

    err = capsys.readouterr().err
    degraded = [line for line in err.splitlines() if line.startswith("DEGRADED:")]
    assert len(degraded) == 1, f"expected exactly one degraded agent, got {degraded}"
    assert "broken" in degraded[0]


def test_check_refuses_the_same_fleet_without_installing_anything(
    fleet: Path, started: list[list[str]], capsys: Any
) -> None:
    """``--check`` is the pre-deploy gate: it reports, installs nothing, starts nothing.

    The module stage is skipped entirely here, so this drives the unreadable
    config through ``print_verify`` alone — the path that decides the exit code.
    """
    with pytest.raises(SystemExit) as exit_info:
        _run("--check")

    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    assert all("UNREADABLE" in row for row in _rows_for(captured.out, "broken"))
    assert "DEGRADED: broken — config unreadable" in captured.err
    assert not (module_root(fleet / "arc") / _MODULE).exists(), "--check installed something"
    assert started == []


# ---------------------------------------------------------------------------
# The stages, individually
# ---------------------------------------------------------------------------


def test_agent_states_records_the_error_and_keeps_reading_the_rest(fleet: Path) -> None:
    """One unreadable config must not truncate the scan.

    ``agent_states`` walks the roster in order and ``broken`` sorts first, so a
    raise rather than a recorded error would hide the healthy agent entirely —
    and an empty ``missing`` list reads as a whole deployment.
    """
    states = {state.agent_id: state for state in up_cmd.agent_states(fleet / "team")}

    assert set(states) == {"broken", "healthy"}
    assert states["broken"].error.startswith("ConfigError")
    assert states["broken"].enabled == ()
    assert states["broken"].degraded is True
    # The healthy agent was still read, and its enabled-but-absent module found.
    assert states["healthy"].error == ""
    assert states["healthy"].enabled == (_MODULE,)
    assert states["healthy"].missing == (_MODULE,)


def test_bootstrap_skips_the_install_for_the_unreadable_agent_only(fleet: Path) -> None:
    """A config that cannot be read cannot say which modules to install.

    Guessing would install a capability nobody enabled (LLM06), so that agent is
    reported and skipped — while the agent whose config *is* readable still gets
    its module.
    """
    rows = up_cmd.bootstrap_modules(up_cmd.agent_states(fleet / "team"))

    broken_rows = [row for row in rows if row[0] == "broken"]
    assert [row[2] for row in broken_rows] == ["UNREADABLE"]
    assert "CONFIG_VALIDATION" in broken_rows[0][3]
    assert next(row[:3] for row in rows if row[1] == _MODULE) == ["healthy", _MODULE, "installed"]
    assert (module_root(fleet / "arc") / _MODULE).is_dir()


def test_print_verify_is_not_whole_when_a_config_is_unreadable(fleet: Path, capsys: Any) -> None:
    """The verdict itself, with no other reason to fail present.

    Every module the readable agent enables is materialized, so ``missing`` is
    empty fleet-wide: the only thing that can turn this False is the unreadable
    config. That is what makes this an assertion about the error branch rather
    than about the missing-module branch that shares the return value.
    """
    up_cmd.bootstrap_modules(up_cmd.agent_states(fleet / "team"))
    states = up_cmd.agent_states(fleet / "team")
    assert all(not state.missing for state in states), "a missing module would confound this"
    capsys.readouterr()

    whole = up_cmd.print_verify(states, [])

    assert whole is False
    captured = capsys.readouterr()
    assert all("UNREADABLE" in row for row in _rows_for(captured.out, "broken"))
    assert "DEGRADED: broken — config unreadable" in captured.err
    assert "healthy" not in captured.err
