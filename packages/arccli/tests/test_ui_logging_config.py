"""Task #38 — `arc ui start` must configure logging so audit events are visible.

Live bug: `UIAuditLogger` emits at INFO on the "arcui.audit" logger, but
`arc ui start` never called `logging.basicConfig()` (or set any logger
level) — Python's interpreter default (root logger effectively WARNING,
no handler) silently dropped every `ui.mutation` / `ui.session_start`
record, plus every adapter connect/auth-reject INFO line
(`arcgateway.adapters.*`, `arcgateway_telegram.adapter`,
`arcgateway_slack.adapter`). Verified live: zero audit lines in journald
despite mutations genuinely happening. `uvicorn.Config(log_level="info")`
only configures uvicorn's OWN loggers — it has no effect on these.
"""

from __future__ import annotations

import logging

import pytest


@pytest.fixture(autouse=True)
def _reset_logging_state() -> None:
    """Each test starts from a clean logging state and restores it after.

    Without this, whichever test runs first would leak its handler/level
    configuration into every later test in the same process — pytest's own
    logging plugin, or an earlier test file, may already have touched the
    root logger.
    """
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_levels = {
        name: logging.getLogger(name).level
        for name in (
            "arcui.audit",
            "arcgateway.adapters",
            "arcgateway_telegram",
            "arcgateway_slack",
            "arcgateway_mattermost",
        )
    }
    yield
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)
    for name, level in saved_levels.items():
        logging.getLogger(name).setLevel(level)


class TestConfigureLogging:
    def test_root_stays_at_warning_by_default(self) -> None:
        from arccli.commands.ui import _configure_logging

        _configure_logging(verbose=False)
        assert logging.getLogger().getEffectiveLevel() == logging.WARNING

    def test_audit_logger_raised_to_info(self) -> None:
        from arccli.commands.ui import _configure_logging

        _configure_logging(verbose=False)
        assert logging.getLogger("arcui.audit").getEffectiveLevel() == logging.INFO

    def test_gateway_adapter_loggers_raised_to_info(self) -> None:
        """Both naming schemes: arcgateway.adapters.* (core) and the
        arcgateway_<platform>.adapter packages (telegram/slack/mattermost,
        which are separate top-level module names, NOT children of
        "arcgateway" — underscore vs dot naming means they need their own
        entries, not just "arcgateway".setLevel()).
        """
        from arccli.commands.ui import _configure_logging

        _configure_logging(verbose=False)
        for name in (
            "arcgateway.adapters.base",
            "arcgateway.adapters.registry",
            "arcgateway.adapters.web",
            "arcgateway_telegram.adapter",
            "arcgateway_slack.adapter",
            "arcgateway_mattermost.adapter",
        ):
            assert logging.getLogger(name).getEffectiveLevel() == logging.INFO, name

    def test_verbose_raises_root_to_info(self) -> None:
        from arccli.commands.ui import _configure_logging

        _configure_logging(verbose=True)
        assert logging.getLogger().getEffectiveLevel() == logging.INFO

    def test_idempotent_even_if_root_already_has_handlers(self) -> None:
        """A prior basicConfig() call (this process, another test, or a
        dependency) must not prevent our config from taking effect —
        arc ui start owns process-wide logging config as the entrypoint.
        """
        logging.basicConfig(level=logging.CRITICAL)  # simulate a prior config
        from arccli.commands.ui import _configure_logging

        _configure_logging(verbose=False)
        assert logging.getLogger("arcui.audit").getEffectiveLevel() == logging.INFO


class TestAuditEventsAreObservable:
    """The actual regression: capture stdout/journal-style output and prove
    a ui.mutation audit event is no longer silently dropped.
    """

    def test_audit_event_reaches_captured_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        from arcui.audit import UIAuditEvent, UIAuditLogger

        from arccli.commands.ui import _configure_logging

        _configure_logging(verbose=False)

        audit = UIAuditLogger(enabled=False)
        audit.audit_event(UIAuditEvent.UI_MUTATION, {"target": "channel:ops", "operation": "x"})

        captured = capsys.readouterr().err
        assert "ui.mutation" in captured
        assert "channel:ops" in captured

    def test_adapter_connect_log_reaches_captured_output(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Simulates the exact live symptom: 'adapter connect logs are
        invisible, network sockets were the only signal.'
        """
        from arccli.commands.ui import _configure_logging

        _configure_logging(verbose=False)

        adapter_logger = logging.getLogger("arcgateway_telegram.adapter")
        adapter_logger.info("TelegramAdapter: connecting (agent_did=%s)", "did:arc:agent:x")

        captured = capsys.readouterr().err
        assert "TelegramAdapter: connecting" in captured

    def test_debug_still_suppressed_without_verbose(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Only INFO+ is raised — DEBUG noise stays off by default."""
        from arccli.commands.ui import _configure_logging

        _configure_logging(verbose=False)

        logging.getLogger("arcui.audit").debug("should not appear")
        captured = capsys.readouterr().err
        assert "should not appear" not in captured
