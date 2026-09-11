"""``arc restart`` — one ordered restart of the whole stack.

Every host action (systemctl, docker, health probe) is patched to a recorder, so
the tests assert the ORDER and the exit code without touching a real node.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from arccli.commands import restart as restart_cmd

R = restart_cmd


def _ok() -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout="", stderr="")


def _fail(stderr: str = "boom") -> SimpleNamespace:
    return SimpleNamespace(returncode=1, stdout="", stderr=stderr)


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch every host action to record an ordered event log; all succeed."""
    calls: list[str] = []
    monkeypatch.setattr(R.shutil, "which", lambda name: f"/usr/bin/{name}")

    def systemctl(*args: str) -> SimpleNamespace:
        calls.append("systemctl " + " ".join(args))
        return _ok()

    def docker_restart(container: str) -> SimpleNamespace:
        calls.append(f"docker restart {container}")
        return _ok()

    def wait_ready(container: str, *, timeout: float) -> bool:
        calls.append(f"wait_ready {container}")
        return True

    monkeypatch.setattr(R, "_systemctl", systemctl)
    monkeypatch.setattr(R, "_docker_restart", docker_restart)
    monkeypatch.setattr(R, "_wait_db_ready", wait_ready)
    # probe_health is imported inside _restart_app from arccli.commands.up.
    import arccli.commands.up as up

    monkeypatch.setattr(
        up, "probe_health", lambda host, port, *, timeout: SimpleNamespace(up=True, detail="ok")
    )
    return calls


def test_services_only_order_no_db(recorder: list[str]) -> None:
    R.restart_handler([])
    # arc.service first, then every companion, and no docker calls at all.
    assert recorder[0] == f"systemctl restart {R.APP_UNIT}"
    assert not any(c.startswith("docker") for c in recorder)
    for unit in R.COMPANION_UNITS:
        assert f"systemctl restart {unit}" in recorder
    # companions come strictly after the app unit
    assert recorder.index(f"systemctl restart {R.COMPANION_UNITS[0]}") > 0


def test_with_db_bounces_containers_before_app(recorder: list[str]) -> None:
    R.restart_handler(["--with-db"])
    first_app = recorder.index(f"systemctl restart {R.APP_UNIT}")
    for container in R.DB_CONTAINERS:
        assert recorder.index(f"docker restart {container}") < first_app
        assert recorder.index(f"wait_ready {container}") < first_app


def test_no_wait_skips_health_probe(monkeypatch: pytest.MonkeyPatch, recorder: list[str]) -> None:
    import arccli.commands.up as up

    def _boom(*a: object, **k: object) -> None:
        raise AssertionError("probe_health must not run under --no-wait")

    monkeypatch.setattr(up, "probe_health", _boom)
    R.restart_handler(["--no-wait"])  # must not raise


def test_app_failure_skips_companions_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, recorder: list[str]
) -> None:
    def systemctl(*args: str) -> SimpleNamespace:
        recorder.append("systemctl " + " ".join(args))
        return _fail() if args == ("restart", R.APP_UNIT) else _ok()

    monkeypatch.setattr(R, "_systemctl", systemctl)
    with pytest.raises(SystemExit) as exc:
        R.restart_handler([])
    assert exc.value.code == 1
    # no companion restart was attempted after the app unit failed
    for unit in R.COMPANION_UNITS:
        assert f"systemctl restart {unit}" not in recorder


def test_unhealthy_app_exits_nonzero(monkeypatch: pytest.MonkeyPatch, recorder: list[str]) -> None:
    import arccli.commands.up as up

    monkeypatch.setattr(
        up, "probe_health", lambda host, port, *, timeout: SimpleNamespace(up=False, detail="down")
    )
    with pytest.raises(SystemExit) as exc:
        R.restart_handler([])
    assert exc.value.code == 1


def test_no_systemctl_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(R.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit) as exc:
        R.restart_handler([])
    assert exc.value.code == 1
