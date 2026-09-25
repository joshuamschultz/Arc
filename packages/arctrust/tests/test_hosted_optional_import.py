"""Hosted account composition remains an optional ArcTrust leaf."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def test_base_trust_imports_when_hosted_files_are_absent(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1] / "src" / "arctrust"
    target = tmp_path / "arctrust"
    shutil.copytree(source, target)
    (target / "hosted_claim.py").unlink()
    (target / "hosted_journal.py").unlink()
    env = {**os.environ, "PYTHONPATH": str(tmp_path)}
    run = subprocess.run(  # noqa: S603  -- fixed interpreter and script, no shell
        [sys.executable, "-c", "import arctrust; assert arctrust.__version__ == '0.12.0'"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert run.returncode == 0, run.stderr
