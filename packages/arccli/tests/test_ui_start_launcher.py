"""Coverage tests for `arc ui start` launcher (review BLOCKER #11).

The previous test suite covered helpers (`_resolve_trace_stores`,
`_maybe_open_browser`, `_select_trace_store`) but not `_start()` itself.
These tests exercise the launcher end-to-end with uvicorn replaced by a
spy so we can assert: (1) atomic 0600 token persistence, (2) loopback
gating of browser open, (3) `mark_bootstrap_issued` only on loopback,
(4) lifespan callback wiring (no monkey-patch), (5) non-loopback
warning + token fallback that does NOT include the auth fragment.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from arccli.commands.ui import (
    BOOTSTRAP_HASH_KEY,
    _maybe_open_browser,
    _persist_agent_token,
    _print_browser_open_fallback,
    _start,
    _TOKEN_FILE,
)


# ---------------------------------------------------------------------------
# C-1: atomic 0600 token persistence
# ---------------------------------------------------------------------------


class TestPersistAgentTokenAtomic:
    """The token file MUST land 0600 from creation, not via post-hoc chmod."""

    def test_file_created_with_0600(self, tmp_path: Path) -> None:
        target = tmp_path / "ui-token"
        with patch(
            "arccli.commands.ui._TOKEN_FILE", target
        ):
            _persist_agent_token("the-secret")
        assert target.exists()
        assert target.read_text() == "the-secret"
        # Mode is OS-level — not subject to the umask race the old code had.
        mode = target.stat().st_mode & 0o777
        assert mode == 0o600, f"expected 0600, got {oct(mode)}"

    def test_parent_dir_is_0700(self, tmp_path: Path) -> None:
        target = tmp_path / "child_dir" / "ui-token"
        with patch(
            "arccli.commands.ui._TOKEN_FILE", target
        ):
            _persist_agent_token("the-secret")
        parent_mode = target.parent.stat().st_mode & 0o777
        assert parent_mode == 0o700, (
            f"parent dir should be 0700 to prevent symlink swap, got "
            f"{oct(parent_mode)}"
        )

    def test_overwrite_preserves_0600(self, tmp_path: Path) -> None:
        target = tmp_path / "ui-token"
        target.write_text("old")
        target.chmod(0o644)  # Worst-case prior state — world-readable.
        with patch(
            "arccli.commands.ui._TOKEN_FILE", target
        ):
            _persist_agent_token("new-secret")
        # The atomic write should also tighten perms on overwrite.
        mode = target.stat().st_mode & 0o777
        assert mode == 0o600
        assert target.read_text() == "new-secret"


# ---------------------------------------------------------------------------
# C-2: browser-open fallback never echoes URL+token together
# ---------------------------------------------------------------------------


class TestBrowserOpenFallback:
    """`_print_browser_open_fallback` must never emit `#auth=...`."""

    def test_fallback_url_carries_no_auth_fragment(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _print_browser_open_fallback(
            "127.0.0.1", 8420, "viewer-token-value", show_tokens=False
        )
        out = capsys.readouterr().out
        assert "viewer-token-value" not in out, (
            "review C-2: token MUST NOT appear in stdout when masked"
        )
        assert "#auth=" not in out, (
            "review C-2: URL+token combination MUST NOT appear in stdout"
        )
        assert "http://127.0.0.1:8420/" in out

    def test_fallback_shows_token_when_show_tokens(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _print_browser_open_fallback(
            "127.0.0.1", 8420, "viewer-token-value", show_tokens=True
        )
        out = capsys.readouterr().out
        # When the operator explicitly asks for tokens, show them — but on
        # a separate line from the URL, never in `#auth=` form.
        assert "viewer-token-value" in out
        assert "#auth=" not in out


# ---------------------------------------------------------------------------
# _maybe_open_browser returns the open-success bool (no stdout side effect)
# ---------------------------------------------------------------------------


class TestMaybeOpenBrowserContract:
    def test_loopback_calls_webbrowser_with_hash(self) -> None:
        with patch("webbrowser.open", return_value=True) as mock_open:
            ok = _maybe_open_browser("127.0.0.1", 8420, "tok-XYZ")
        assert ok is True
        url = mock_open.call_args[0][0]
        assert f"#{BOOTSTRAP_HASH_KEY}=tok-XYZ" in url

    def test_non_loopback_returns_false_without_calling_webbrowser(self) -> None:
        with patch("webbrowser.open") as mock_open:
            ok = _maybe_open_browser("0.0.0.0", 8420, "tok")
        assert ok is False
        mock_open.assert_not_called()

    def test_oserror_returns_false_silently(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch("webbrowser.open", side_effect=OSError("no browser")):
            ok = _maybe_open_browser("127.0.0.1", 8420, "tok")
        assert ok is False
        # MUST NOT print URL with token on failure (review C-2).
        out = capsys.readouterr().out
        assert "tok" not in out
        assert "#auth=" not in out


# ---------------------------------------------------------------------------
# S-1: _start uses Starlette on_startup, not server.startup monkey-patch
# ---------------------------------------------------------------------------


def _make_args(**kw: object) -> argparse.Namespace:
    base = {
        "host": "127.0.0.1",
        "port": 18420,
        "viewer_token": "v",
        "operator_token": "o",
        "agent_token": "a",
        "max_agents": 10,
        "show_tokens": False,
        "root": None,
    }
    base.update(kw)
    return argparse.Namespace(**base)


class TestStartLoopback:
    """Loopback `arc ui start` opens browser via on_startup, marks bootstrap."""

    def test_registers_on_startup_callback_no_monkey_patch(
        self, tmp_path: Path
    ) -> None:
        captured = {}

        class _SpyServer:
            def __init__(self, config):
                captured["app"] = config.app

            def run(self):
                pass  # don't actually serve

        with patch("arccli.commands.ui._TOKEN_FILE", tmp_path / "ui-token"), \
             patch("arccli.commands.ui._resolve_trace_stores", return_value=[]), \
             patch("uvicorn.Server", _SpyServer), \
             patch("arccli.commands.ui._maybe_open_browser") as mock_open:
            _start(_make_args(host="127.0.0.1"))

        app = captured["app"]
        # The browser-open callback must be registered on Starlette's
        # lifespan via app.state._extra_startup_hooks (Wave 2 TD-04
        # migration off the deprecated `on_startup=` param). The lifespan
        # context manager invokes everything in this list before yielding.
        hooks = app.state._extra_startup_hooks
        assert any(
            cb.__name__ == "_open_browser_on_ready" for cb in hooks
        ), "loopback launch should register an extra startup hook"
        # Browser is NOT opened synchronously — only when the lifespan fires.
        mock_open.assert_not_called()

    def test_marks_bootstrap_token_for_session_audit(
        self, tmp_path: Path
    ) -> None:
        captured = {}

        class _SpyServer:
            def __init__(self, config):
                captured["app"] = config.app

            def run(self):
                pass

        with patch("arccli.commands.ui._TOKEN_FILE", tmp_path / "ui-token"), \
             patch("arccli.commands.ui._resolve_trace_stores", return_value=[]), \
             patch("uvicorn.Server", _SpyServer):
            _start(_make_args(host="127.0.0.1"))

        tracker = captured["app"].state.session_tracker
        # The viewer token should be marked as `browser_bootstrap` so
        # AuthMiddleware emits ui.session_start with the right auth_method.
        result = tracker.observe("v", "127.0.0.1")
        assert result is not None
        _session_id, auth_method = result
        assert auth_method == "browser_bootstrap"


class TestStartNonLoopback:
    """Non-loopback bind MUST NOT open the browser, MUST NOT mark bootstrap."""

    def test_no_on_startup_callback_added(self, tmp_path: Path) -> None:
        captured = {}

        class _SpyServer:
            def __init__(self, config):
                captured["app"] = config.app

            def run(self):
                pass

        with patch("arccli.commands.ui._TOKEN_FILE", tmp_path / "ui-token"), \
             patch("arccli.commands.ui._resolve_trace_stores", return_value=[]), \
             patch("uvicorn.Server", _SpyServer):
            _start(_make_args(host="0.0.0.0"))

        app = captured["app"]
        # _open_browser_on_ready hook should NOT be registered.
        hooks = app.state._extra_startup_hooks
        assert not any(
            cb.__name__ == "_open_browser_on_ready" for cb in hooks
        ), "non-loopback launch must NOT register browser-open hook"

    def test_does_not_mark_bootstrap(self, tmp_path: Path) -> None:
        captured = {}

        class _SpyServer:
            def __init__(self, config):
                captured["app"] = config.app

            def run(self):
                pass

        with patch("arccli.commands.ui._TOKEN_FILE", tmp_path / "ui-token"), \
             patch("arccli.commands.ui._resolve_trace_stores", return_value=[]), \
             patch("uvicorn.Server", _SpyServer):
            _start(_make_args(host="0.0.0.0"))

        tracker = captured["app"].state.session_tracker
        result = tracker.observe("v", "1.2.3.4")
        assert result is not None
        _session_id, auth_method = result
        # No bootstrap mark → manual_token, even though token is valid.
        assert auth_method == "manual_token"

    def test_prints_warning(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        class _SpyServer:
            def __init__(self, config):
                pass

            def run(self):
                pass

        with patch("arccli.commands.ui._TOKEN_FILE", tmp_path / "ui-token"), \
             patch("arccli.commands.ui._resolve_trace_stores", return_value=[]), \
             patch("uvicorn.Server", _SpyServer):
            _start(_make_args(host="0.0.0.0"))
        out = capsys.readouterr().out
        assert "non-loopback" in out.lower()
