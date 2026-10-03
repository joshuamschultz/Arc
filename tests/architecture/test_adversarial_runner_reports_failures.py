"""The adversarial battery runner must always say which suite failed and why.

A non-zero exit with no pytest output once left an operator with nothing to act on.
The runner now asks pytest for a machine report and prints its own verdict, so a
failure, a crash before pytest wrote anything, and a run that collected nothing are
each named on stderr and each exit non-zero.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

_RUNNER = Path(__file__).resolve().parents[1] / "run_adversarial_tests.py"


@pytest.fixture
def runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location("run_adversarial_tests", _RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    return module


def _suite(runner: ModuleType, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    (runner.ROOT / "test_fake_suite.py").write_text(body)
    monkeypatch.setattr(runner, "SCENARIOS", {"fake threat": ("test_fake_suite.py",)})


def _report_path(command: list[str]) -> Path:
    return Path(next(a for a in command if a.startswith("--junitxml=")).split("=", 1)[1])


def test_a_failing_test_names_its_suite_and_reason(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _suite(runner, monkeypatch, "def test_boom():\n    assert 1 == 2, 'nope'\n")
    code = runner.main(["-p", "no:cacheprovider"])
    err = capsys.readouterr().err
    assert code != 0
    assert "test_fake_suite::test_boom" in err and "test_boom" in err and "nope" in err


def test_a_crash_with_no_report_is_named_not_silent(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _suite(runner, monkeypatch, "def test_ok():\n    pass\n")
    monkeypatch.setattr(runner.subprocess, "run", lambda c, **_: subprocess.CompletedProcess(c, 1))
    code = runner.main([])
    err = capsys.readouterr().err
    assert code == 1
    assert "exited 1" in err and "no test report" in err


def test_a_run_that_collects_nothing_fails_loudly(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _suite(runner, monkeypatch, "def test_ok():\n    pass\n")

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        _report_path(command).write_text('<testsuites><testsuite tests="0"/></testsuites>')
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    assert runner.main([]) != 0
    assert "ran no tests" in capsys.readouterr().err


def test_a_clean_run_exits_zero(runner: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    _suite(runner, monkeypatch, "def test_ok():\n    pass\n")
    assert runner.main(["-p", "no:cacheprovider"]) == 0
