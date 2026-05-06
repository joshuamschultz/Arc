"""Shell-level tests for the shared agent-manifest reader.

These run the `_read_agent_manifest` shell function in isolation by sourcing
``deploy/lib/agent-manifest.sh`` with a fake REPO_ROOT, so we can exercise
the malformed-entry branch without invoking the rest of the deploy.
SPEC-025 §M3 + §TD-4 (extracted to a shared lib for AWS + Azure).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_MANIFEST_LIB = _REPO_ROOT / "deploy" / "lib" / "agent-manifest.sh"


def _run_manifest(manifest_text: str | None, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Source the shared manifest lib against a synthetic manifest file.

    Writes ``manifest_text`` (or omits the file if None) to
    ``tmp_path/agents.enabled`` and runs the shell function.
    Returns the CompletedProcess so callers can assert on returncode/stderr.
    """
    manifest_path = tmp_path / "agents.enabled"
    if manifest_text is not None:
        manifest_path.write_text(manifest_text, encoding="utf-8")
    snippet = (
        "set -u\n"
        f". '{_MANIFEST_LIB}'\n"
        f"_read_agent_manifest '{manifest_path}'\n"
    )
    return subprocess.run(
        ["/bin/bash", "-c", snippet],  # absolute path to satisfy S607
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )


def test_manifest_with_well_formed_entries_passes(tmp_path: Path) -> None:
    """A normal manifest returns the entries on stdout, exit 0."""
    proc = _run_manifest("nlit_cora_agent\nnlit_soc_agent\nscap_isso_agent\n", tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "nlit_cora_agent" in proc.stdout
    assert "scap_isso_agent" in proc.stdout


def test_manifest_with_path_traversal_aborts(tmp_path: Path) -> None:
    """SPEC-025 §M3 — `../../../etc_agent` is rejected, exit 1."""
    proc = _run_manifest("scap_isso_agent\n../../../etc_agent\n", tmp_path)
    assert proc.returncode != 0
    assert "malformed entry" in proc.stderr


def test_manifest_with_uppercase_entry_aborts(tmp_path: Path) -> None:
    """Uppercase or special chars must fail the strict shape check."""
    proc = _run_manifest("scap_isso_agent\nBAD_Agent\n", tmp_path)
    assert proc.returncode != 0
    assert "malformed entry" in proc.stderr


def test_manifest_with_shell_metachar_aborts(tmp_path: Path) -> None:
    """Shell metacharacters are rejected before any rm -rf could see them."""
    proc = _run_manifest("scap_isso_agent\nfoo;rm_-rf_agent\n", tmp_path)
    assert proc.returncode != 0
    assert "malformed entry" in proc.stderr


def test_manifest_missing_falls_back_to_default(tmp_path: Path) -> None:
    """Missing file = warn + use default trio (operator-on-fresh-VM convenience)."""
    proc = _run_manifest(None, tmp_path)
    assert proc.returncode == 0
    assert "nlit_cora_agent" in proc.stdout
    assert "WARN" in proc.stderr


def test_manifest_empty_aborts(tmp_path: Path) -> None:
    """Empty manifest = misconfiguration, exit 1."""
    proc = _run_manifest("# only comments\n\n", tmp_path)
    assert proc.returncode != 0
    assert "empty" in proc.stderr
