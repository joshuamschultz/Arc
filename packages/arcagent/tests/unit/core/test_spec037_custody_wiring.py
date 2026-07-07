"""SPEC-037 F1/F2 — custody is wired end-to-end and the tier crypto floor holds.

F1: under ``custody=vault_transit`` the agent's operator authority is a
``VaultSigner`` and the operator SEED is never loaded into the agent process
(``_operator_key is None``); the policy WORM chain still signs and verifies
against the transit public key.

F2: ``tier=federal`` couples require_fips + vault_transit + ecdsa-p256, failing
closed on any explicitly weaker override.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arctrust import keypair, verify_chain
from arctrust.signer import (
    ECDSA_P256,
    ED25519,
    FileNotaryTransit,
    SignerError,
    VaultSigner,
)

from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    LLMConfig,
    SecurityConfig,
    TelemetryConfig,
)
from arcagent.core.tool_policy import PolicyContext, ToolCall


def _vault_transit_config(tmp_path: Path, *, algorithm: str, tier: str) -> ArcAgentConfig:
    cfg = ArcAgentConfig(
        agent=AgentConfig(name="vt-agent", workspace=str(tmp_path / "ws")),
        llm=LLMConfig(model="test/model"),
        telemetry=TelemetryConfig(enabled=False),
    )
    cfg.security.tier = tier
    cfg.security.custody = "vault_transit"
    cfg.security.signing_algorithm = algorithm
    cfg.security.operator_key_dir = str(tmp_path / "operator")
    cfg.security.notary_keystore = str(tmp_path / "notary")
    cfg.security.witness_medium_path = str(tmp_path / "witness" / "anchor.log")
    return cfg


def _provision_notary(tmp_path: Path, algorithm: str) -> bytes:
    """Provision the out-of-process notary keystore; return the operator pubkey."""
    seed = keypair.generate_keypair().private_key
    FileNotaryTransit.provision(tmp_path / "notary", "operator", seed, algorithm=algorithm)
    transit = FileNotaryTransit(tmp_path / "notary", algorithm=algorithm)
    return transit.public_key("operator")


def _unsigned_call(agent_did: str) -> ToolCall:
    return ToolCall(
        tool_name="read_file",
        arguments={"path": "/tmp/x"},
        agent_did=agent_did,
        session_id="s1",
        classification="unclassified",
    )


class TestF1VaultTransitWiring:
    async def test_operator_seed_never_in_agent_process(self, tmp_path: Path) -> None:
        from arcagent.core.agent import ArcAgent

        op_pub = _provision_notary(tmp_path, ED25519)
        cfg = _vault_transit_config(tmp_path, algorithm=ED25519, tier="enterprise")
        agent = ArcAgent(config=cfg, config_path=tmp_path / "arcagent.toml")
        await agent.startup()
        try:
            # The operator authority signs BY REFERENCE — no seed in this process.
            assert isinstance(agent._operator_signer, VaultSigner)
            assert agent._operator_key is None
            assert agent._operator_signer.public_key == op_pub

            # The policy chain still records a tamper-evident, operator-signed WORM
            # record, verifiable ONLY under the transit public key.
            identity = agent._identity
            assert identity is not None
            ctx = PolicyContext(tier="enterprise", policy_version="1.0", bundle_age_seconds=0.0)
            decision = await agent._policy_pipeline.evaluate(_unsigned_call(identity.did), ctx)
            assert decision.is_deny()
            path = agent._policy_audit_log_path()
            agent_pub = identity.public_key
        finally:
            await agent.shutdown()

        assert path.exists()
        assert verify_chain(path, op_pub) is True
        assert verify_chain(path, agent_pub) is False

    async def test_federal_vault_transit_ecdsa_end_to_end(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from arcagent.core.agent import ArcAgent

        # A federal deployment links a CMVP OpenSSL FIPS provider; emulate it so
        # the FIPS floor passes and ECDSA-P256 out-of-process signing runs E2E.
        monkeypatch.setattr("arctrust.fips.fips_backend_active", lambda: True)
        op_pub = _provision_notary(tmp_path, ECDSA_P256)
        cfg = _vault_transit_config(tmp_path, algorithm=ECDSA_P256, tier="federal")
        agent = ArcAgent(config=cfg, config_path=tmp_path / "arcagent.toml")
        await agent.startup()
        try:
            assert isinstance(agent._operator_signer, VaultSigner)
            assert agent._operator_signer.algorithm == ECDSA_P256
            assert agent._operator_key is None
            identity = agent._identity
            assert identity is not None
            ctx = PolicyContext(tier="federal", policy_version="1.0", bundle_age_seconds=0.0)
            await agent._policy_pipeline.evaluate(_unsigned_call(identity.did), ctx)
            path = agent._policy_audit_log_path()
        finally:
            await agent.shutdown()
        assert verify_chain(path, op_pub) is True

    async def test_vault_transit_unresolvable_transit_fails_closed(self, tmp_path: Path) -> None:
        """A vault_transit config with no provisioned notary must NOT boot with an
        in-process seed — it fails closed (NFR-3)."""
        from arcagent.core.agent import ArcAgent

        cfg = _vault_transit_config(tmp_path, algorithm=ED25519, tier="enterprise")
        # Note: notary keystore was never provisioned.
        agent = ArcAgent(config=cfg, config_path=tmp_path / "arcagent.toml")
        with pytest.raises(SignerError):  # startup must reject, not fall back
            await agent.startup()
        assert agent._operator_key is None


class TestF2FederalTierFloor:
    def test_federal_forces_fips_vault_ecdsa(self) -> None:
        sec = SecurityConfig(tier="federal")
        assert sec.require_fips is True
        assert sec.custody == "vault_transit"
        assert sec.signing_algorithm == "ecdsa-p256"

    def test_federal_rejects_explicit_non_fips(self) -> None:
        with pytest.raises(ValueError, match="require_fips"):
            SecurityConfig(tier="federal", require_fips=False)

    def test_federal_rejects_explicit_in_process(self) -> None:
        with pytest.raises(ValueError, match="vault_transit"):
            SecurityConfig(tier="federal", custody="in_process")

    def test_federal_rejects_explicit_ed25519(self) -> None:
        with pytest.raises(ValueError, match="ecdsa-p256"):
            SecurityConfig(tier="federal", signing_algorithm="ed25519")

    def test_enterprise_defaults_vault_transit_relaxable(self) -> None:
        assert SecurityConfig(tier="enterprise").custody == "vault_transit"
        assert SecurityConfig(tier="enterprise", custody="in_process").custody == "in_process"

    def test_personal_stays_in_process(self) -> None:
        assert SecurityConfig(tier="personal").custody == "in_process"
        assert SecurityConfig(tier="personal").require_fips is False
