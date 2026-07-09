"""Coverage tests for `arc ui start` launcher (review BLOCKER #11).

The previous test suite covered helpers (`_maybe_open_browser`) but not
`_start()` itself.
These tests exercise the launcher end-to-end with uvicorn replaced by a
spy so we can assert: (1) loopback gating of browser open, (2)
`mark_bootstrap_issued` only on loopback, (3) lifespan callback wiring
(no monkey-patch), (4) non-loopback warning + token fallback that does
NOT include the auth fragment.
"""

from __future__ import annotations

import argparse
from unittest.mock import patch

import pytest

from arccli.commands.ui import (
    BOOTSTRAP_HASH_KEY,
    _maybe_open_browser,
    _print_browser_open_fallback,
    _start,
)

# ---------------------------------------------------------------------------
# C-2: browser-open fallback never echoes URL+token together
# ---------------------------------------------------------------------------


class TestBrowserOpenFallback:
    """`_print_browser_open_fallback` is a token-free nudge to the link above."""

    def test_fallback_is_a_token_free_nudge(self, capsys: pytest.CaptureFixture[str]) -> None:
        _print_browser_open_fallback("127.0.0.1", 8420)
        out = capsys.readouterr().out
        # The fallback itself carries no token/hash — `_start` already
        # printed the full magic-link above on the loopback path.
        assert "#auth=" not in out
        assert "http://127.0.0.1:8420/" in out


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
            assert _maybe_open_browser("0.0.0.0", 8420, "tok") is False  # noqa: S104
        mock_open.assert_not_called()

    def test_oserror_returns_false_silently(self, capsys: pytest.CaptureFixture[str]) -> None:
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
        "max_agents": 10,
        "show_tokens": False,
        "root": None,
    }
    base.update(kw)
    return argparse.Namespace(**base)


class TestStartLoopback:
    """Loopback `arc ui start` opens browser via on_startup, marks bootstrap."""

    def test_registers_on_startup_callback_no_monkey_patch(self) -> None:
        captured = {}

        class _SpyServer:
            def __init__(self, config):
                captured["app"] = config.app

            def run(self):
                pass  # don't actually serve

        with (
            patch("uvicorn.Server", _SpyServer),
            patch("arccli.commands.ui._maybe_open_browser") as mock_open,
        ):
            _start(_make_args(host="127.0.0.1"))

        app = captured["app"]
        # The browser-open callback must be registered on Starlette's
        # lifespan via app.state._extra_startup_hooks (Wave 2 TD-04
        # migration off the deprecated `on_startup=` param). The lifespan
        # context manager invokes everything in this list before yielding.
        hooks = app.state._extra_startup_hooks
        assert any(cb.__name__ == "_open_browser_on_ready" for cb in hooks), (
            "loopback launch should register an extra startup hook"
        )
        # Browser is NOT opened synchronously — only when the lifespan fires.
        mock_open.assert_not_called()

    def test_loopback_banner_prints_full_token_and_magic_link(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        full_viewer = "a" * 64  # a realistic-length token, not "v"

        class _SpyServer:
            def __init__(self, config: object) -> None:
                pass

            def run(self) -> None:
                pass

        with (
            patch("uvicorn.Server", _SpyServer),
            patch("arccli.commands.ui._maybe_open_browser"),
        ):
            _start(_make_args(host="127.0.0.1", viewer_token=full_viewer))

        out = capsys.readouterr().out
        # Symptom 3: the FULL viewer token is printed, not truncated.
        assert full_viewer in out
        assert "..." not in out.split(full_viewer)[0].splitlines()[-1]
        # Symptom 2: a working magic-link with the token in the hash.
        assert f"http://127.0.0.1:18420/#{BOOTSTRAP_HASH_KEY}={full_viewer}" in out

    def test_marks_bootstrap_token_for_session_audit(self) -> None:
        captured = {}

        class _SpyServer:
            def __init__(self, config):
                captured["app"] = config.app

            def run(self):
                pass

        with patch("uvicorn.Server", _SpyServer):
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

    def test_no_on_startup_callback_added(self) -> None:
        captured = {}

        class _SpyServer:
            def __init__(self, config):
                captured["app"] = config.app

            def run(self):
                pass

        with patch("uvicorn.Server", _SpyServer):
            _start(_make_args(host="0.0.0.0"))  # noqa: S104

        app = captured["app"]
        # _open_browser_on_ready hook should NOT be registered.
        hooks = app.state._extra_startup_hooks
        assert not any(cb.__name__ == "_open_browser_on_ready" for cb in hooks), (
            "non-loopback launch must NOT register browser-open hook"
        )

    def test_does_not_mark_bootstrap(self) -> None:
        captured = {}

        class _SpyServer:
            def __init__(self, config):
                captured["app"] = config.app

            def run(self):
                pass

        with patch("uvicorn.Server", _SpyServer):
            _start(_make_args(host="0.0.0.0"))  # noqa: S104

        tracker = captured["app"].state.session_tracker
        result = tracker.observe("v", "1.2.3.4")
        assert result is not None
        _session_id, auth_method = result
        # No bootstrap mark → manual_token, even though token is valid.
        assert auth_method == "manual_token"

    def test_prints_warning(self, capsys: pytest.CaptureFixture[str]) -> None:
        class _SpyServer:
            def __init__(self, config):
                pass

            def run(self):
                pass

        with patch("uvicorn.Server", _SpyServer):
            _start(_make_args(host="0.0.0.0"))  # noqa: S104
        out = capsys.readouterr().out
        assert "non-loopback" in out.lower()

    def test_non_loopback_masks_token_and_omits_magic_link(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        full_viewer = "b" * 64

        class _SpyServer:
            def __init__(self, config: object) -> None:
                pass

            def run(self) -> None:
                pass

        with patch("uvicorn.Server", _SpyServer):
            _start(_make_args(host="0.0.0.0", viewer_token=full_viewer))  # noqa: S104
        out = capsys.readouterr().out
        # Non-loopback stays strict: token masked, no token-bearing URL.
        assert full_viewer not in out
        assert "#auth=" not in out
