"""Smoke tests for arc init via subprocess.

These tests verify that `arc init --tier <tier> --provider <provider>`
produces expected output and exits correctly. They are the regression net
for the T1.1.5 migration.

Non-interactive paths only: --tier and --provider flags prevent prompts.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_ARC = Path(__file__).parent.parent.parent.parent / ".venv" / "bin" / "arc"


def _arc(*args: str) -> subprocess.CompletedProcess[str]:
    """Run `arc <args>` and return the CompletedProcess."""
    return subprocess.run(
        [str(_ARC), *args],
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# arc init --help
# ---------------------------------------------------------------------------


class TestInitHelp:
    def test_help_exits_zero(self) -> None:
        """arc init --help exits 0."""
        result = _arc("init", "--help")
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_help_shows_tier_option(self) -> None:
        """arc init --help shows --tier option."""
        result = _arc("init", "--help")
        assert "--tier" in result.stdout

    def test_help_shows_provider_option(self) -> None:
        """arc init --help shows --provider option."""
        result = _arc("init", "--help")
        assert "--provider" in result.stdout

    def test_help_shows_quick_option(self) -> None:
        """arc init --help shows --quick option."""
        result = _arc("init", "--help")
        assert "--quick" in result.stdout


# ---------------------------------------------------------------------------
# arc init --tier open --provider anthropic --dir <tmp>
# ---------------------------------------------------------------------------


class TestInitTierOpen:
    def test_init_open_exits_zero(self, tmp_path: Path) -> None:
        """arc init --tier open --provider anthropic exits 0."""
        result = _arc(
            "init", "--tier", "open", "--provider", "anthropic", "--dir", str(tmp_path)
        )
        assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"

    def test_init_open_output_nonempty(self, tmp_path: Path) -> None:
        """arc init produces non-empty stdout."""
        result = _arc(
            "init", "--tier", "open", "--provider", "anthropic", "--dir", str(tmp_path)
        )
        assert result.stdout.strip()

    def test_init_open_writes_config(self, tmp_path: Path) -> None:
        """arc init --tier open writes arcllm.toml (primary config file)."""
        _arc("init", "--tier", "open", "--provider", "anthropic", "--dir", str(tmp_path))
        assert (tmp_path / "arcllm.toml").exists()

    def test_init_open_config_has_tier(self, tmp_path: Path) -> None:
        """arc init --tier open writes tier comment into arcllm.toml."""
        _arc("init", "--tier", "open", "--provider", "anthropic", "--dir", str(tmp_path))
        content = (tmp_path / "arcllm.toml").read_text()
        assert "open" in content

    def test_init_shows_summary(self, tmp_path: Path) -> None:
        """arc init shows configuration summary."""
        result = _arc(
            "init", "--tier", "open", "--provider", "anthropic", "--dir", str(tmp_path)
        )
        assert "Tier" in result.stdout or "tier" in result.stdout.lower()


class TestInitTierEnterprise:
    def test_init_enterprise_exits_zero(self, tmp_path: Path) -> None:
        """arc init --tier enterprise exits 0."""
        result = _arc(
            "init", "--tier", "enterprise", "--provider", "openai", "--dir", str(tmp_path)
        )
        assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"

    def test_init_enterprise_writes_config(self, tmp_path: Path) -> None:
        """arc init --tier enterprise writes arcllm.toml (primary config file)."""
        _arc("init", "--tier", "enterprise", "--provider", "openai", "--dir", str(tmp_path))
        assert (tmp_path / "arcllm.toml").exists()


class TestInitQuick:
    def test_init_quick_exits_zero(self, tmp_path: Path) -> None:
        """arc init --quick --provider anthropic exits 0."""
        result = _arc("init", "--quick", "--provider", "anthropic", "--dir", str(tmp_path))
        assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"

    def test_init_quick_writes_config(self, tmp_path: Path) -> None:
        """arc init --quick writes arcllm.toml (primary config file)."""
        _arc("init", "--quick", "--provider", "anthropic", "--dir", str(tmp_path))
        assert (tmp_path / "arcllm.toml").exists()


# Mark to avoid unused import warning
_ = pytest
