"""``runtime_extensions_dir`` pins the runtime version the process booted on."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from arctrust import paths


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(paths, "_BOOT_RUNTIME_EXTENSIONS", {})
    root = tmp_path / "runtime"
    for version in ("v1", "v2"):
        (root / version / "extensions").mkdir(parents=True)
    (root / "current").symlink_to("v1")
    return tmp_path


def _flip(home: Path, version: str) -> None:
    link = home / "runtime" / "current"
    link.unlink()
    link.symlink_to(version)


def test_flipping_current_after_start_keeps_boot_version(home: Path) -> None:
    first = paths.runtime_extensions_dir()
    _flip(home, "v2")
    assert paths.runtime_extensions_dir() == first
    assert first == (home / "runtime" / "v1" / "extensions").resolve()


def test_swap_between_verify_and_load_never_reaches_new_code(home: Path) -> None:
    verified = paths.runtime_extensions_dir()
    _flip(home, "v2")
    assert paths.runtime_extensions_dir() == verified


def test_fresh_process_uses_new_version(home: Path) -> None:
    paths.runtime_extensions_dir()
    _flip(home, "v2")
    out = subprocess.run(  # noqa: S603 - fixed argv, own interpreter
        [
            sys.executable,
            "-c",
            "from arctrust import paths; print(paths.runtime_extensions_dir())",
        ],
        env={**os.environ, "ARC_CONFIG_DIR": str(home)},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert Path(out) == (home / "runtime" / "v2" / "extensions").resolve()
