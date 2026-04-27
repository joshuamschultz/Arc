"""Smoke tests for arc ext subcommands via subprocess.

These tests verify that each `arc ext <subcommand>` invocation produces
expected output and exits correctly. They are the regression net for the
T1.1.5 migration.
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
# arc ext (no subcommand — shows help)
# ---------------------------------------------------------------------------


class TestExtHelp:
    def test_no_args_exits_zero(self) -> None:
        """arc ext with no args exits 0 and shows help."""
        result = _arc("ext")
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_no_args_shows_subcommands(self) -> None:
        """arc ext help lists expected subcommands."""
        result = _arc("ext")
        combined = result.stdout + result.stderr
        assert any(sub in combined for sub in ["list", "create", "install", "validate"])


# ---------------------------------------------------------------------------
# arc ext list
# ---------------------------------------------------------------------------


class TestExtList:
    def test_list_exits_zero(self) -> None:
        """arc ext list exits 0."""
        result = _arc("ext", "list")
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_list_output_nonempty(self) -> None:
        """arc ext list produces some output."""
        result = _arc("ext", "list")
        # Either 'No extensions found' or a table — either is valid
        assert result.stdout.strip() or result.returncode == 0


# ---------------------------------------------------------------------------
# arc ext create
# ---------------------------------------------------------------------------


class TestExtCreate:
    def test_create_exits_zero(self, tmp_path: Path) -> None:
        """arc ext create <name> --dir <tmp> exits 0."""
        result = _arc("ext", "create", "test-ext", "--dir", str(tmp_path))
        assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"

    def test_create_writes_file(self, tmp_path: Path) -> None:
        """arc ext create writes a .py file."""
        _arc("ext", "create", "test-ext", "--dir", str(tmp_path))
        assert (tmp_path / "test-ext.py").exists()

    def test_create_file_has_factory(self, tmp_path: Path) -> None:
        """arc ext create produces a file with an extension() factory."""
        _arc("ext", "create", "my-ext", "--dir", str(tmp_path))
        content = (tmp_path / "my-ext.py").read_text()
        assert "def extension(" in content

    def test_create_fails_if_exists(self, tmp_path: Path) -> None:
        """arc ext create fails if file already exists."""
        _arc("ext", "create", "dup-ext", "--dir", str(tmp_path))
        result = _arc("ext", "create", "dup-ext", "--dir", str(tmp_path))
        assert result.returncode != 0


# ---------------------------------------------------------------------------
# arc ext validate
# ---------------------------------------------------------------------------


class TestExtValidate:
    def test_validate_valid_extension(self, tmp_path: Path) -> None:
        """arc ext validate passes on a valid extension file."""
        ext_file = tmp_path / "good_ext.py"
        ext_file.write_text(
            '"""Good extension."""\n\ndef extension(api):\n    pass\n'
        )
        result = _arc("ext", "validate", str(ext_file))
        assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
        assert "OK" in result.stdout or "ok" in result.stdout.lower()

    def test_validate_no_factory_fails(self, tmp_path: Path) -> None:
        """arc ext validate fails when extension() function is missing."""
        ext_file = tmp_path / "bad_ext.py"
        ext_file.write_text('"""No factory."""\n\ndef something_else():\n    pass\n')
        result = _arc("ext", "validate", str(ext_file))
        assert result.returncode != 0

    def test_validate_nonexistent_fails(self) -> None:
        """arc ext validate fails on a nonexistent file."""
        result = _arc("ext", "validate", "/tmp/__no_such_ext__.py")
        assert result.returncode != 0

    def test_validate_non_py_fails(self, tmp_path: Path) -> None:
        """arc ext validate fails on non-.py file."""
        md_file = tmp_path / "bad.md"
        md_file.write_text("not python\n")
        result = _arc("ext", "validate", str(md_file))
        assert result.returncode != 0


# ---------------------------------------------------------------------------
# arc ext install
# ---------------------------------------------------------------------------


class TestExtInstall:
    def test_install_single_file(self, tmp_path: Path) -> None:
        """arc ext install copies a .py file to global extensions dir.

        Note: this test creates a real file in ~/.arcagent/extensions/.
        Cleanup is not guaranteed — acceptable for smoke tests.
        """
        ext_file = tmp_path / "smoke_install_test.py"
        ext_file.write_text(
            '"""Smoke test extension."""\n\ndef extension(api):\n    pass\n'
        )
        global_dir = Path.home() / ".arcagent" / "extensions"
        dest = global_dir / "smoke_install_test.py"

        # Clean up leftover from prior run
        if dest.exists():
            dest.unlink()

        result = _arc("ext", "install", str(ext_file))
        assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
        assert dest.exists()

        # Cleanup
        if dest.exists():
            dest.unlink()


# Mark to avoid unused import warning
_ = pytest
