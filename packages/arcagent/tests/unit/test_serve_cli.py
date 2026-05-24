"""Tests for ``python -m arcagent serve`` CLI helpers."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from arcagent.__main__ import (
    _build_parser,
    _load_config,
    _stringify_response,
    _write_shutdown_marker,
    main,
)


@pytest.fixture()
def valid_agent_dir(tmp_path: Path) -> Path:
    """Minimal agent directory with the smallest legal arcagent.toml."""
    (tmp_path / "arcagent.toml").write_text(
        textwrap.dedent(
            """\
            [agent]
            name = "serve-test"

            [llm]
            model = "openai/gpt-4o-mini"
            """
        )
    )
    return tmp_path


class TestParser:
    def test_serve_requires_agent_dir(self) -> None:
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["serve"])

    def test_serve_defaults_to_stdin_inbound(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["serve", "/some/path"])
        assert args.command == "serve"
        assert args.inbound == "stdin"

    def test_serve_accepts_none_inbound(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["serve", "/some/path", "--inbound", "none"])
        assert args.inbound == "none"

    def test_serve_rejects_unknown_inbound(self) -> None:
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["serve", "/p", "--inbound", "nats"])


class TestLoadConfig:
    def test_loads_valid_toml(self, valid_agent_dir: Path) -> None:
        config, path = _load_config(valid_agent_dir)
        assert config.agent.name == "serve-test"
        assert path == valid_agent_dir / "arcagent.toml"

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="arcagent.toml not found"):
            _load_config(tmp_path)


class TestShutdownMarker:
    def test_writes_clean_marker(self, tmp_path: Path) -> None:
        _write_shutdown_marker(tmp_path, status="clean", reason="sigterm")
        payload = json.loads((tmp_path / ".arc-shutdown.json").read_text())
        assert payload["status"] == "clean"
        assert payload["reason"] == "sigterm"
        assert isinstance(payload["exit_at"], (int, float))

    def test_writes_crash_marker(self, tmp_path: Path) -> None:
        _write_shutdown_marker(tmp_path, status="crashed", reason="exception")
        payload = json.loads((tmp_path / ".arc-shutdown.json").read_text())
        assert payload["status"] == "crashed"


class TestStringifyResponse:
    def test_none_returns_empty(self) -> None:
        assert _stringify_response(None) == ""

    def test_plain_string(self) -> None:
        assert _stringify_response("hi") == "hi"

    def test_strips_newlines(self) -> None:
        # Stdout protocol is line-delimited; embedded newlines would
        # confuse the supervisor that wraps us.
        assert _stringify_response("line1\nline2") == "line1 line2"

    def test_object_with_content_attr(self) -> None:
        class _R:
            content = "hello\nworld"

        assert _stringify_response(_R()) == "hello world"

    def test_object_fallback_to_str(self) -> None:
        class _R:
            def __str__(self) -> str:
                return "{repr}"

        assert _stringify_response(_R()) == "{repr}"


class TestMainErrorPaths:
    def test_missing_directory_returns_nonzero(self, tmp_path: Path) -> None:
        exit_code = main(["serve", str(tmp_path / "does-not-exist")])
        assert exit_code == 2
