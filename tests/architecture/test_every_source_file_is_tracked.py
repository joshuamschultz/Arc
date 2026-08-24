"""Every shipped source file must be in the repository.

A file that exists on one machine and in no commit passes every test that
machine runs and is absent from every wheel built anywhere else. That is how
``arccli.formatting`` went missing: it was moved down into arcagent, recreated
in place, and never added — so the CLI's entry point could not import and the
deploy refused to activate the runtime it had just built.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]


def _tracked() -> set[Path]:
    listing = subprocess.run(
        ["git", "ls-files", "-z", "--", "packages", "extensions"],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    return {_REPO / name for name in listing.stdout.split("\0") if name}


def test_no_shipped_python_file_is_untracked() -> None:
    tracked = _tracked()
    assert len(tracked) > 500, "git listing collapsed; this guard would pass vacuously"

    untracked: list[str] = []
    for package in sorted((_REPO / "packages").iterdir()):
        source = package / "src"
        if not source.is_dir():
            continue
        for path in source.rglob("*.py"):
            if "__pycache__" in path.parts or ".venv" in path.parts:
                continue
            if path not in tracked:
                untracked.append(str(path.relative_to(_REPO)))

    assert not untracked, (
        "these files ship in a wheel but are in no commit, so every machine but "
        "this one builds without them:\n  " + "\n  ".join(sorted(untracked))
    )
