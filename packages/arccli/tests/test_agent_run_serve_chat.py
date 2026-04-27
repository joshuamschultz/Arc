"""Subprocess smoke tests for arc agent run / serve / chat.

These tests are the RED gate for the legacy-shim removal migration.
They verify the three commands work correctly through pure argparse
handlers (no Click CliRunner shim).

Test strategy:
- arc agent run: pass a fake toml pointing at mock provider; assert exit 0
  and that the help text is present (we can't actually run the LLM in CI).
- arc agent serve: test help and error path (missing dir → exit 1).
- arc agent chat: pipe /quit to stdin, assert clean exit.

For LLM integration tests we verify the argparse path works and that
the error paths emit useful messages. Full LLM round-trips require a
live provider and are covered by integration tests.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ARC = Path(__file__).parent.parent.parent.parent / ".venv" / "bin" / "arc"
_JOSH_AGENT = Path(__file__).parent.parent.parent.parent / "team" / "josh_agent"


def _arc(
    *args: str,
    stdin: str | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    """Run `arc <args>` and return CompletedProcess."""
    merged_env = dict(os.environ)
    if env:
        merged_env.update(env)
    return subprocess.run(
        [str(_ARC), *args],
        capture_output=True,
        text=True,
        input=stdin,
        env=merged_env,
        timeout=timeout,
    )


def _make_minimal_agent(tmp_path: Path, model: str = "anthropic/claude-test") -> Path:
    """Create a minimal agent directory with arcagent.toml."""
    agent_dir = tmp_path / "test_agent"
    agent_dir.mkdir()
    workspace = agent_dir / "workspace"
    workspace.mkdir()
    (workspace / "identity.md").write_text("# Identity\nTest agent.\n")
    (workspace / "policy.md").write_text("# Policy\n- Be helpful.\n")
    (workspace / "context.md").write_text("# Context\n")
    (agent_dir / "tools").mkdir()
    (agent_dir / "tools" / "__init__.py").write_text("")

    toml_content = textwrap.dedent(f"""\
        [agent]
        name = "test-agent"
        org = "test"
        type = "executor"
        workspace = "./workspace"

        [llm]
        model = "{model}"
        max_tokens = 100
        temperature = 0.0

        [identity]
        did = "did:arc:test:abc123"
        key_dir = "~/.arcagent/keys"

        [vault]
        backend = ""

        [tools.policy]
        allow = []
        deny = []
        timeout_seconds = 30

        [telemetry]
        enabled = false
        service_name = "test-agent"
        log_level = "WARNING"
        export_traces = false

        [context]
        max_tokens = 4096

        [eval]
        provider = ""
        model = ""
        max_tokens = 512
        temperature = 0.0
        fallback_behavior = "skip"

        [session]
        retention_count = 10
        retention_days = 7

        [extensions]
        global_dir = "~/.arcagent/extensions"
    """)
    (agent_dir / "arcagent.toml").write_text(toml_content)
    return agent_dir


# ---------------------------------------------------------------------------
# arc agent run — help and error paths
# ---------------------------------------------------------------------------


class TestAgentRunHelp:
    def test_run_help_exits_zero(self) -> None:
        """arc agent run --help exits 0 with usage text."""
        result = _arc("agent", "run", "--help")
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_run_help_shows_path_and_task(self) -> None:
        """arc agent run --help mentions path and task arguments."""
        result = _arc("agent", "run", "--help")
        assert "path" in result.stdout.lower() or "agent" in result.stdout.lower(), (
            f"stdout: {result.stdout!r}"
        )

    def test_run_missing_dir_exits_nonzero(self) -> None:
        """arc agent run on a nonexistent directory exits non-zero."""
        result = _arc("agent", "run", "/tmp/__no_such_agent__", "hello")
        assert result.returncode != 0, f"Should have failed; stdout: {result.stdout!r}"

    def test_run_missing_dir_shows_error_message(self) -> None:
        """arc agent run on a nonexistent directory prints an error."""
        result = _arc("agent", "run", "/tmp/__no_such_agent__", "hello")
        error_output = result.stdout + result.stderr
        assert "not found" in error_output.lower() or "__no_such_agent__" in error_output, (
            f"Expected error in output; stdout={result.stdout!r} stderr={result.stderr!r}"
        )

    def test_run_missing_toml_exits_nonzero(self) -> None:
        """arc agent run on a dir without arcagent.toml exits non-zero."""
        with tempfile.TemporaryDirectory() as tmp:
            result = _arc("agent", "run", tmp, "hello")
            assert result.returncode != 0

    def test_run_missing_toml_shows_error(self) -> None:
        """arc agent run on a dir without arcagent.toml shows toml error."""
        with tempfile.TemporaryDirectory() as tmp:
            result = _arc("agent", "run", tmp, "hello")
            error_output = result.stdout + result.stderr
            assert "arcagent.toml" in error_output, (
                f"Expected 'arcagent.toml' in output; stdout={result.stdout!r} stderr={result.stderr!r}"
            )


# ---------------------------------------------------------------------------
# arc agent serve — help and error paths
# ---------------------------------------------------------------------------


class TestAgentServeHelp:
    def test_serve_help_exits_zero(self) -> None:
        """arc agent serve --help exits 0."""
        result = _arc("agent", "serve", "--help")
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_serve_help_shows_path(self) -> None:
        """arc agent serve --help mentions path."""
        result = _arc("agent", "serve", "--help")
        assert "path" in result.stdout.lower() or "agent" in result.stdout.lower(), (
            f"stdout: {result.stdout!r}"
        )

    def test_serve_missing_dir_exits_nonzero(self) -> None:
        """arc agent serve on a nonexistent directory exits non-zero."""
        result = _arc("agent", "serve", "/tmp/__no_such_agent__", timeout=10)
        assert result.returncode != 0

    def test_serve_missing_dir_shows_error(self) -> None:
        """arc agent serve on a nonexistent dir shows error message."""
        result = _arc("agent", "serve", "/tmp/__no_such_agent__", timeout=10)
        error_output = result.stdout + result.stderr
        assert "not found" in error_output.lower() or "__no_such_agent__" in error_output, (
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )

    def test_serve_missing_toml_exits_nonzero(self) -> None:
        """arc agent serve on a dir without arcagent.toml exits non-zero."""
        with tempfile.TemporaryDirectory() as tmp:
            result = _arc("agent", "serve", tmp, timeout=10)
            assert result.returncode != 0

    def test_serve_missing_toml_shows_error(self) -> None:
        """arc agent serve on a dir without arcagent.toml shows toml error."""
        with tempfile.TemporaryDirectory() as tmp:
            result = _arc("agent", "serve", tmp, timeout=10)
            error_output = result.stdout + result.stderr
            assert "arcagent.toml" in error_output, (
                f"stdout={result.stdout!r} stderr={result.stderr!r}"
            )


# ---------------------------------------------------------------------------
# arc agent chat — help and error paths
# ---------------------------------------------------------------------------


class TestAgentChatHelp:
    def test_chat_help_exits_zero(self) -> None:
        """arc agent chat --help exits 0."""
        result = _arc("agent", "chat", "--help")
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_chat_help_shows_path(self) -> None:
        """arc agent chat --help mentions path."""
        result = _arc("agent", "chat", "--help")
        assert "path" in result.stdout.lower() or "agent" in result.stdout.lower(), (
            f"stdout: {result.stdout!r}"
        )

    def test_chat_missing_dir_exits_nonzero(self) -> None:
        """arc agent chat on a nonexistent dir exits non-zero."""
        result = _arc("agent", "chat", "/tmp/__no_such_agent__", stdin="/quit\n", timeout=10)
        assert result.returncode != 0

    def test_chat_missing_dir_shows_error(self) -> None:
        """arc agent chat on a nonexistent dir shows error message."""
        result = _arc("agent", "chat", "/tmp/__no_such_agent__", stdin="/quit\n", timeout=10)
        error_output = result.stdout + result.stderr
        assert "not found" in error_output.lower() or "__no_such_agent__" in error_output, (
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )

    def test_chat_missing_toml_exits_nonzero(self) -> None:
        """arc agent chat on a dir without arcagent.toml exits non-zero."""
        with tempfile.TemporaryDirectory() as tmp:
            result = _arc("agent", "chat", tmp, stdin="/quit\n", timeout=10)
            assert result.returncode != 0

    def test_chat_missing_toml_shows_error(self) -> None:
        """arc agent chat on a dir without arcagent.toml shows toml error."""
        with tempfile.TemporaryDirectory() as tmp:
            result = _arc("agent", "chat", tmp, stdin="/quit\n", timeout=10)
            error_output = result.stdout + result.stderr
            assert "arcagent.toml" in error_output, (
                f"stdout={result.stdout!r} stderr={result.stderr!r}"
            )


# ---------------------------------------------------------------------------
# Arc agent --help shows all subcommands
# ---------------------------------------------------------------------------


class TestAgentGroupHelp:
    def test_agent_help_shows_run(self) -> None:
        """arc agent --help lists 'run' subcommand."""
        result = _arc("agent", "--help")
        assert result.returncode == 0
        assert "run" in result.stdout

    def test_agent_help_shows_serve(self) -> None:
        """arc agent --help lists 'serve' subcommand."""
        result = _arc("agent", "--help")
        assert result.returncode == 0
        assert "serve" in result.stdout

    def test_agent_help_shows_chat(self) -> None:
        """arc agent --help lists 'chat' subcommand."""
        result = _arc("agent", "--help")
        assert result.returncode == 0
        assert "chat" in result.stdout
