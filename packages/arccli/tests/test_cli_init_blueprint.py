"""SPEC-047 Phase 5 — `arc init --blueprint` materializes the merged config to disk.

init writes the user-wide ~/.arc/arcagent.toml (the layered CLI/gateway base). The
blueprint is deep-merged UNDER the init defaults and the tier floored by stringency-max.
(The flat-read agent-runtime boot is proven via `arc blueprint apply --agent DIR` in the
P6 AC-3 E2E — DC-8b: __main__ flat-reads the per-agent file, not ~/.arc.)
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path


def _arc(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "arccli", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_init_blueprint_writes_merged_arcagent_config(tmp_path: Path) -> None:
    result = _arc(
        "init", "--tier", "personal", "--provider", "anthropic",
        "--blueprint", "personal-assistant", "--dir", str(tmp_path),
    )
    assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
    written = tomllib.loads((tmp_path / "arcagent.toml").read_text(encoding="utf-8"))
    assert written["modules"]["memory"]["config"]["brain"] == "arcmemory"
    assert written["security"]["tier"] == "personal"


def test_init_blueprint_floors_tier_up(tmp_path: Path) -> None:
    # personal deployment + federal blueprint -> federal (blueprint can only RAISE).
    result = _arc(
        "init", "--tier", "personal", "--provider", "anthropic",
        "--blueprint", "federal-analyst", "--dir", str(tmp_path),
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    written = tomllib.loads((tmp_path / "arcagent.toml").read_text(encoding="utf-8"))
    assert written["security"]["tier"] == "federal"
    assert "raised from" in result.stdout


def test_init_rejects_open_tier(tmp_path: Path) -> None:
    # 'open' is fully removed — no legacy alias.
    result = _arc("init", "--tier", "open", "--provider", "anthropic", "--dir", str(tmp_path))
    assert result.returncode != 0


def test_init_arcllm_toml_uses_personal_vocab(tmp_path: Path) -> None:
    _arc("init", "--tier", "personal", "--provider", "anthropic", "--dir", str(tmp_path))
    content = (tmp_path / "arcllm.toml").read_text(encoding="utf-8")
    assert "personal" in content
    assert "open" not in content
