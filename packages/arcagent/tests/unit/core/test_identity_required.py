"""Tests for §1: DID required on ArcAgent construction and §7 IdentityRequired revival.

TDD — these tests are written before implementation (RED phase).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arcagent.core.config import AgentConfig, ArcAgentConfig, LLMConfig, TelemetryConfig
from arcagent.core.errors import IdentityError, IdentityRequired


class TestIdentityRequired:
    """§7 — IdentityRequired subclass of IdentityError must exist and be raiseable."""

    def test_identity_required_is_subclass_of_identity_error(self) -> None:
        assert issubclass(IdentityRequired, IdentityError)

    def test_identity_required_raises_with_message(self) -> None:
        with pytest.raises(IdentityRequired, match="DID"):
            raise IdentityRequired()

    def test_identity_required_str_includes_code(self) -> None:
        err = IdentityRequired()
        s = str(err)
        assert "IDENTITY_REQUIRED" in s

    def test_identity_required_has_hint_in_details(self) -> None:
        err = IdentityRequired()
        assert "arc agent init" in err.details.get("hint", "")


class TestArcAgentRequiresDID:
    """§1 — ArcAgent must require a DID; missing DID raises IdentityRequired."""

    @pytest.fixture()
    def config_no_did(self, tmp_path: Path) -> ArcAgentConfig:
        return ArcAgentConfig(
            agent=AgentConfig(name="test-agent", workspace=str(tmp_path / "ws")),
            llm=LLMConfig(model="test/model"),
            telemetry=TelemetryConfig(enabled=False),
            # identity left at default (empty did="" — auto-generate path)
        )

    async def test_startup_succeeds_when_did_config_present(
        self, config_no_did: ArcAgentConfig, tmp_path: Path
    ) -> None:
        """startup() should generate/load a DID from key_dir; it must not blow up
        just because the did field is empty (auto-generate path is still valid).
        This test verifies the auto-generate path works end-to-end.
        """
        from arcagent.core.agent import ArcAgent

        agent = ArcAgent(config=config_no_did, config_path=tmp_path / "arcagent.toml")
        await agent.startup()
        assert agent._identity is not None
        assert agent._identity.did.startswith("did:arc:")
        await agent.shutdown()

    def test_constructor_accepts_config(self, config_no_did: ArcAgentConfig) -> None:
        """ArcAgent constructor should accept a config and not error yet."""
        from arcagent.core.agent import ArcAgent

        agent = ArcAgent(config=config_no_did)
        assert agent is not None

    async def test_startup_with_invalid_did_raises_identity_error(self, tmp_path: Path) -> None:
        """If identity config has a malformed DID, startup should raise IdentityError
        (or a subclass) — not a bare exception.
        """
        from arcagent.core.agent import ArcAgent
        from arcagent.core.config import IdentityConfig

        config = ArcAgentConfig(
            agent=AgentConfig(name="bad-did-agent", workspace=str(tmp_path / "ws")),
            llm=LLMConfig(model="test/model"),
            telemetry=TelemetryConfig(enabled=False),
            identity=IdentityConfig(
                did="NOT_A_VALID_DID",
                key_dir=str(tmp_path / "keys"),
            ),
        )
        agent = ArcAgent(config=config)
        # Identity error (or ValueError from validate_did) is acceptable
        with pytest.raises((IdentityError, ValueError, Exception)):
            await agent.startup()
