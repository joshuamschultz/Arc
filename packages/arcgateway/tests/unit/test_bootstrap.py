"""Tests for arcgateway.bootstrap.build_for_embedded.

The composition root is a thin wiring layer — these tests focus on
which adapter slots are populated for which config combinations and on
the executor selection by tier.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arcgateway.bootstrap import (
    EmbeddedGateway,
    _extract_agent_name,
    build_for_embedded,
)
from arcgateway.config import GatewayConfig
from arcgateway.executor import AsyncioExecutor


@pytest.fixture
def empty_team_root(tmp_path: Path) -> Path:
    return tmp_path / "team"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _config(toml: str) -> GatewayConfig:
    return GatewayConfig.from_toml_str(toml)


# ── DID parsing ───────────────────────────────────────────────────────────────


def test_extract_agent_name_did_arc_agent_form() -> None:
    assert _extract_agent_name("did:arc:agent:concierge") == "concierge"


def test_extract_agent_name_did_arc_org_form() -> None:
    assert _extract_agent_name("did:arc:org:agent/concierge") == "concierge"


def test_extract_agent_name_default_did() -> None:
    assert _extract_agent_name("did:arc:agent:default") == "default"


# ── Build cases ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_for_embedded_web_only(empty_team_root: Path) -> None:
    """With only [platforms.web].enabled, only the web slot is populated."""
    cfg = _config(
        """
[gateway]
agent_did = "did:arc:agent:default"

[platforms.web]
enabled = true
"""
    )
    bundle = await build_for_embedded(empty_team_root, cfg)
    assert isinstance(bundle, EmbeddedGateway)
    assert bundle.web_adapter is not None
    assert bundle.slack_adapter is None
    assert bundle.telegram_adapter is None
    assert isinstance(bundle.executor, AsyncioExecutor)
    assert bundle.web_adapter.name == "web"


@pytest.mark.asyncio
async def test_build_for_embedded_no_platforms(empty_team_root: Path) -> None:
    """An empty config still yields a usable executor + session_router."""
    cfg = _config("")
    bundle = await build_for_embedded(empty_team_root, cfg)
    assert bundle.web_adapter is None
    assert bundle.slack_adapter is None
    assert bundle.telegram_adapter is None
    assert isinstance(bundle.executor, AsyncioExecutor)


@pytest.mark.asyncio
async def test_build_for_embedded_federal_tier_uses_subprocess_executor(
    empty_team_root: Path,
) -> None:
    """Federal tier swaps in SubprocessExecutor for OS-level isolation."""
    cfg = _config(
        """
[gateway]
tier = "federal"
"""
    )
    bundle = await build_for_embedded(empty_team_root, cfg)
    # SubprocessExecutor is the federal-tier choice — type name match is
    # sufficient (avoids importing the class here, which would couple the
    # test to internal subprocess plumbing).
    assert type(bundle.executor).__name__ == "SubprocessExecutor"


@pytest.mark.asyncio
async def test_build_for_embedded_slack_skipped_without_tokens(
    empty_team_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slack enabled but no env tokens → adapter is silently skipped."""
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    cfg = _config(
        """
[platforms.slack]
enabled = true
"""
    )
    bundle = await build_for_embedded(empty_team_root, cfg)
    assert bundle.slack_adapter is None


@pytest.mark.asyncio
async def test_build_for_embedded_telegram_skipped_without_token(
    empty_team_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Telegram enabled but no token env var → adapter is silently skipped."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    cfg = _config(
        """
[platforms.telegram]
enabled = true
"""
    )
    bundle = await build_for_embedded(empty_team_root, cfg)
    assert bundle.telegram_adapter is None


@pytest.mark.asyncio
async def test_build_for_embedded_web_slack_telegram_together(
    empty_team_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All three platforms enabled with mock tokens → all three slots populated.

    Skips when the optional adapter packages are not installed (kept lean
    in the test environment).
    """
    pytest.importorskip("slack_bolt")
    pytest.importorskip("telegram")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-1234")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-1234")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1234:test-token")
    cfg = _config(
        """
[gateway]
agent_did = "did:arc:agent:default"

[platforms.web]
enabled = true

[platforms.slack]
enabled = true

[platforms.telegram]
enabled = true
"""
    )
    bundle = await build_for_embedded(empty_team_root, cfg)
    assert bundle.web_adapter is not None
    assert bundle.slack_adapter is not None
    assert bundle.telegram_adapter is not None
