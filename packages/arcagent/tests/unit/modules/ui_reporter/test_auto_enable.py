"""Tests for UIReporter auto-enable probe (SPEC-019 T4.1, T4.2).

The probe runs at module startup when `enabled` is True (the default).
Each branch produces a distinct reason string so an auditor can tell
"no UI running" from "operator forgot to chmod 0600".

`enabled = false` is the only opt-out; it short-circuits the probe entirely.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from arcagent.modules.ui_reporter import (
    UIReporterConfig,
    UIReporterModule,
    _open_token_file_secure,
    _server_reachable,
    _should_auto_enable,
)


_LOOPBACK_URL = "ws://127.0.0.1:8420/api/agent/connect"


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    """A token file with correct perms and ownership."""
    f = tmp_path / "ui-token"
    f.write_text("secret-token")
    f.chmod(0o600)
    return f


# ---------------------------------------------------------------------------
# _open_token_file_secure — TOCTOU-safe single-fd read (review H-3)
# ---------------------------------------------------------------------------


class TestOpenTokenFileSecure:
    """The probe must read perms+UID and contents from one fstat'd fd."""

    def test_absent(self, tmp_path: Path) -> None:
        token, reason = _open_token_file_secure(tmp_path / "nope")
        assert token is None
        assert reason == "token_file_absent"

    def test_loose_perms(self, token_file: Path) -> None:
        token_file.chmod(0o644)
        token, reason = _open_token_file_secure(token_file)
        assert token is None
        assert reason == "token_file_loose_perms"

    def test_wrong_owner(self, token_file: Path) -> None:
        real = os.fstat(os.open(str(token_file), os.O_RDONLY))

        class _FakeStat:
            st_uid = real.st_uid + 1
            st_mode = real.st_mode

        with patch("os.fstat", return_value=_FakeStat()):
            token, reason = _open_token_file_secure(token_file)
        assert token is None
        assert reason == "token_file_wrong_owner"

    def test_happy_path_returns_bytes(self, token_file: Path) -> None:
        token, reason = _open_token_file_secure(token_file)
        assert reason == "ok"
        assert token == "secret-token"

    def test_strips_whitespace(self, tmp_path: Path) -> None:
        f = tmp_path / "ui-token"
        f.write_text("  trimmed-token\n")
        f.chmod(0o600)
        token, reason = _open_token_file_secure(f)
        assert reason == "ok"
        assert token == "trimmed-token"


# ---------------------------------------------------------------------------
# _server_reachable — HTTP probe pinned to loopback (review H-4)
# ---------------------------------------------------------------------------


class TestServerReachable:
    def test_rejects_non_loopback_host(self) -> None:
        ok, reason = _server_reachable(
            "ws://10.0.0.5:8420/api/agent/connect"
        )
        assert ok is False
        assert reason == "url_not_loopback"

    def test_rejects_remote_dns_name(self) -> None:
        ok, reason = _server_reachable(
            "wss://attacker.example.com/api/agent/connect"
        )
        assert ok is False
        assert reason == "url_not_loopback"

    def test_accepts_127_0_0_1(self) -> None:
        with patch("httpx.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__enter__.return_value
            response = MagicMock()
            response.status_code = 200
            mock_client.head.return_value = response
            ok, reason = _server_reachable(_LOOPBACK_URL)
        assert ok is True
        assert reason == "probe_ok"

    def test_accepts_localhost(self) -> None:
        with patch("httpx.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__enter__.return_value
            response = MagicMock()
            response.status_code = 200
            mock_client.head.return_value = response
            ok, _ = _server_reachable(
                "ws://localhost:8420/api/agent/connect"
            )
        assert ok is True

    def test_connection_error(self) -> None:
        with patch("httpx.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__enter__.return_value
            mock_client.head.side_effect = httpx.ConnectError("refused")
            ok, reason = _server_reachable(_LOOPBACK_URL)
        assert ok is False
        assert reason.startswith("probe_failed_")

    def test_404_rejects(self) -> None:
        with patch("httpx.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__enter__.return_value
            response = MagicMock()
            response.status_code = 404
            mock_client.head.return_value = response
            ok, reason = _server_reachable(_LOOPBACK_URL)
        assert ok is False
        assert reason == "probe_status_404"

    def test_405_accepted(self) -> None:
        # 405 (HEAD-not-allowed on a GET-only route) still proves the route
        # is reachable, so the probe must accept it.
        with patch("httpx.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__enter__.return_value
            response = MagicMock()
            response.status_code = 405
            mock_client.head.return_value = response
            ok, _ = _server_reachable(_LOOPBACK_URL)
        assert ok is True


# ---------------------------------------------------------------------------
# _should_auto_enable composes the two — and returns the read token bytes
# ---------------------------------------------------------------------------


class TestShouldAutoEnableComposition:
    def test_returns_three_tuple_with_token_on_success(
        self, token_file: Path
    ) -> None:
        with patch("httpx.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__enter__.return_value
            response = MagicMock()
            response.status_code = 200
            mock_client.head.return_value = response
            enable, reason, token = _should_auto_enable(
                token_file, _LOOPBACK_URL
            )
        assert enable is True
        assert reason == "probe_ok"
        # Token MUST come from the same fd that validated perms (H-3).
        assert token == "secret-token"

    def test_file_failure_yields_no_token(self, tmp_path: Path) -> None:
        enable, reason, token = _should_auto_enable(
            tmp_path / "absent", _LOOPBACK_URL
        )
        assert enable is False
        assert reason == "token_file_absent"
        assert token is None

    def test_server_failure_yields_no_token(self, token_file: Path) -> None:
        with patch("httpx.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__enter__.return_value
            mock_client.head.side_effect = httpx.ConnectError("refused")
            enable, reason, token = _should_auto_enable(
                token_file, _LOOPBACK_URL
            )
        assert enable is False
        assert reason.startswith("probe_failed_")
        assert token is None

    def test_non_loopback_url_rejected_even_with_good_file(
        self, token_file: Path
    ) -> None:
        # Review H-4: a poisoned config can't redirect autoconnect to an
        # attacker host. _should_auto_enable refuses non-loopback URLs
        # before any HTTP request is made.
        with patch("httpx.Client") as mock_client_cls:
            enable, reason, token = _should_auto_enable(
                token_file, "ws://10.0.0.5:8420/api/agent/connect"
            )
        assert enable is False
        assert reason == "url_not_loopback"
        assert token is None
        mock_client_cls.assert_not_called()


# ---------------------------------------------------------------------------
# UIReporterConfig.enabled: bool default True
# ---------------------------------------------------------------------------


class TestUIReporterConfigEnabled:
    def test_default_is_true(self) -> None:
        cfg = UIReporterConfig()
        assert cfg.enabled is True

    def test_explicit_false(self) -> None:
        cfg = UIReporterConfig(enabled=False)
        assert cfg.enabled is False


# ---------------------------------------------------------------------------
# Startup branching: enabled=False short-circuits; enabled=True runs the probe.
# ---------------------------------------------------------------------------


class TestStartupBranching:
    async def test_explicit_false_returns_early(self, tmp_path: Path) -> None:
        """No probe, no transport when enabled=False."""
        module = UIReporterModule(
            config={"enabled": False, "url": _LOOPBACK_URL},
            workspace=tmp_path,
        )
        ctx = MagicMock()
        ctx.config = MagicMock()
        ctx.config.agent = MagicMock()
        ctx.config.agent.name = "x"

        with patch(
            "arcagent.modules.ui_reporter._should_auto_enable"
        ) as mock_probe:
            await module.startup(ctx)
        mock_probe.assert_not_called()
        assert module._transport is None

    async def test_default_runs_probe(self, tmp_path: Path) -> None:
        """Default config (enabled=True) MUST run the probe."""
        module = UIReporterModule(
            config={"url": _LOOPBACK_URL},
            workspace=tmp_path,
        )
        ctx = MagicMock()
        ctx.config = MagicMock()
        ctx.config.agent = MagicMock()
        ctx.config.agent.name = "x"

        with patch(
            "arcagent.modules.ui_reporter._should_auto_enable",
            return_value=(False, "token_file_absent", None),
        ) as mock_probe:
            await module.startup(ctx)
        mock_probe.assert_called_once()


class TestAgentAutoconnectAuditFields:
    """SR-3 / T5.2: ui.agent_autoconnect MUST carry agent_id, uid, url, reason.

    Without all four, federal auditors can't trace which agent connected
    where, by which OS user, on what evidence.
    """

    async def test_all_four_fields_emitted_on_successful_probe(
        self, tmp_path: Path
    ) -> None:
        import os

        emitted: list[tuple[str, dict]] = []

        def _capture(event: str, details: dict) -> None:
            emitted.append((event, details))

        module = UIReporterModule(
            config={"url": _LOOPBACK_URL, "token": "stub"},
            workspace=tmp_path,
        )
        ctx = MagicMock()
        ctx.bus = MagicMock()
        ctx.config = MagicMock()
        ctx.config.agent = MagicMock()
        ctx.config.agent.name = "test_agent"
        ctx.config.agent.did = "did:arc:test"
        ctx.config.llm = MagicMock()
        ctx.config.llm.model = "anthropic/claude"
        ctx.config.modules = {}
        ctx.telemetry = MagicMock()
        ctx.telemetry.audit_event = _capture

        with patch(
            "arcagent.modules.ui_reporter._should_auto_enable",
            return_value=(True, "probe_ok", "stub-token"),
        ):
            await module.startup(ctx)

        autoconnects = [d for e, d in emitted if e == "ui.agent_autoconnect"]
        assert len(autoconnects) == 1
        details = autoconnects[0]
        assert details["agent_id"] == "did:arc:test"
        assert details["uid"] == os.getuid()
        assert details["url"] == _LOOPBACK_URL
        assert details["reason"] == "probe_ok"
