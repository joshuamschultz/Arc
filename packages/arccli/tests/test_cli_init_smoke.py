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
# arc init --tier personal --provider anthropic --dir <tmp>
# ---------------------------------------------------------------------------


class TestInitTierPersonal:
    def test_init_personal_exits_zero(self, tmp_path: Path) -> None:
        """arc init --tier personal --provider anthropic exits 0."""
        result = _arc("init", "--tier", "personal", "--provider", "anthropic", "--dir", str(tmp_path))
        assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"

    def test_init_personal_output_nonempty(self, tmp_path: Path) -> None:
        """arc init produces non-empty stdout."""
        result = _arc("init", "--tier", "personal", "--provider", "anthropic", "--dir", str(tmp_path))
        assert result.stdout.strip()

    def test_init_personal_writes_config(self, tmp_path: Path) -> None:
        """arc init --tier personal writes arcllm.toml (primary config file)."""
        _arc("init", "--tier", "personal", "--provider", "anthropic", "--dir", str(tmp_path))
        assert (tmp_path / "arcllm.toml").exists()

    def test_init_personal_config_has_tier(self, tmp_path: Path) -> None:
        """arc init --tier personal writes tier comment into arcllm.toml."""
        _arc("init", "--tier", "personal", "--provider", "anthropic", "--dir", str(tmp_path))
        content = (tmp_path / "arcllm.toml").read_text()
        assert "personal" in content

    def test_init_shows_summary(self, tmp_path: Path) -> None:
        """arc init shows configuration summary."""
        result = _arc("init", "--tier", "personal", "--provider", "anthropic", "--dir", str(tmp_path))
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

class TestInitQuickNonInteractive:
    """F3 — `--quick` must never prompt (dies on EOF non-interactively otherwise).

    Handler-level so it runs without the installed `arc` binary. Any `input()`
    call under --quick is a regression, so we monkeypatch it to blow up.
    """

    @staticmethod
    def _no_input(monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*_a: object, **_k: object) -> str:
            raise AssertionError("--quick must not prompt via input()")

        monkeypatch.setattr("builtins.input", _boom)

    def test_quick_does_not_prompt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from arccli.commands.init import init_handler

        self._no_input(monkeypatch)
        init_handler(["--quick", "--dir", str(tmp_path)])  # must not raise
        assert (tmp_path / "arcllm.toml").exists()

    def test_quick_defaults_provider_anthropic(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from arccli.commands.init import init_handler

        self._no_input(monkeypatch)
        init_handler(["--quick", "--dir", str(tmp_path)])
        assert 'provider = "anthropic"' in (tmp_path / "arcllm.toml").read_text()

    def test_quick_honors_provider_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from arccli.commands.init import init_handler

        self._no_input(monkeypatch)
        init_handler(["--quick", "--provider", "openai", "--dir", str(tmp_path)])
        assert 'provider = "openai"' in (tmp_path / "arcllm.toml").read_text()


# Mark to avoid unused import warning
_ = pytest
